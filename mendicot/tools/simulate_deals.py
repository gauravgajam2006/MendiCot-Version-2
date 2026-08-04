"""Offline statistical simulator for the production secure deal pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
import json
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from mendicot.deck import (
    CANONICAL_DECK,
    DEALING_ALGORITHM_VERSION,
    DECK_DEFINITION_VERSION,
    deal_cards,
    validate_deal_invariants,
)
from mendicot.enums import Rank, Suit
from mendicot.models import Card, Player
from mendicot.secure_shuffle import (
    SHUFFLE_ALGORITHM_VERSION,
    SecureShuffleService,
    attach_dealt_hands,
    verify_shuffle_audit,
)


REPORT_SCHEMA_VERSION = "mendicot-deal-simulation-v1"
PLAYER_COUNTS = (4, 6, 8)
RED_SUITS = frozenset((Suit.HEARTS, Suit.DIAMONDS))
ServiceFactory = Callable[[], SecureShuffleService]


def _card_key(card: Card) -> str:
    return f"{card.suit.value}:{card.rank.value}"


def _players(player_count: int) -> list[Player]:
    return [
        Player(
            player_id=f"P{seat_index}",
            team_id="TeamA" if seat_index % 2 == 0 else "TeamB",
            seat_index=seat_index,
        )
        for seat_index in range(player_count)
    ]


def _timing_summary(samples_ns: Sequence[int]) -> dict[str, float | int]:
    if not samples_ns:
        return {"samples": 0, "average_ms": 0.0, "max_ms": 0.0}
    return {
        "samples": len(samples_ns),
        "average_ms": round(sum(samples_ns) / len(samples_ns) / 1_000_000, 6),
        "max_ms": round(max(samples_ns) / 1_000_000, 6),
    }


def _histogram(counter: Counter[int], maximum: int) -> dict[str, int]:
    return {str(value): counter[value] for value in range(maximum + 1)}


def _observed_range(counter: Counter[int]) -> dict[str, int | None]:
    observed = [value for value, count in counter.items() if count]
    return {
        "min": min(observed) if observed else None,
        "max": max(observed) if observed else None,
    }


def _simulate_mode(
    deals: int,
    player_count: int,
    service_factory: ServiceFactory,
) -> dict[str, Any]:
    service = service_factory()
    players = _players(player_count)
    ordered_ids = [player.player_id for player in players]
    expected_cards = set(CANONICAL_DECK)
    hand_size = 48 // player_count
    card_keys = [_card_key(card) for card in CANONICAL_DECK]

    position_frequency = {key: [0] * 48 for key in card_keys}
    seat_card_frequency = {
        str(seat): {key: 0 for key in card_keys}
        for seat in range(player_count)
    }
    suit_counts = {suit.value: Counter() for suit in Suit}
    red_counts: Counter[int] = Counter()
    black_counts: Counter[int] = Counter()
    mendis_per_hand: Counter[int] = Counter()
    mendis_per_team = {"TeamA": Counter(), "TeamB": Counter()}
    invariant_failures: Counter[str] = Counter()
    shuffle_times: list[int] = []
    deal_times: list[int] = []
    verification_times: list[int] = []
    total_times: list[int] = []
    commitment_verification_failures = 0
    completed_deals = 0

    for deal_index in range(deals):
        total_started = perf_counter_ns()
        try:
            shuffle_started = perf_counter_ns()
            try:
                shuffled, audit = service.prepare_shuffle(
                    game_id=f"simulation-{player_count}-{deal_index}",
                    room_id=f"simulation-{player_count}",
                    players=players,
                    trump_mode="normal",
                    selected_first_player_id=ordered_ids[0],
                )
            except Exception:
                invariant_failures["shuffle_failure"] += 1
                invariant_failures["deals_with_any_failure"] += 1
                continue
            finally:
                shuffle_times.append(perf_counter_ns() - shuffle_started)

            deal_flags: set[str] = set()
            if len(shuffled) != 48:
                deal_flags.add("shuffled_card_count")
            if len(set(shuffled)) != len(shuffled):
                deal_flags.add("shuffled_duplicates")
            shuffled_cards = set(shuffled)
            if expected_cards - shuffled_cards:
                deal_flags.add("shuffled_missing_cards")
            if shuffled_cards - expected_cards:
                deal_flags.add("shuffled_unexpected_cards")

            for position, card in enumerate(shuffled):
                key = _card_key(card)
                if key in position_frequency and position < 48:
                    position_frequency[key][position] += 1

            hands_list: list[list[Card]] | None = None
            deal_started = perf_counter_ns()
            try:
                hands_list = deal_cards(shuffled, player_count)
                hands = {
                    player_id: hands_list[seat]
                    for seat, player_id in enumerate(ordered_ids)
                }
                flattened = [card for hand in hands_list for card in hand]
                if any(len(hand) != hand_size for hand in hands_list):
                    deal_flags.add("hand_size")
                if len(flattened) != 48:
                    deal_flags.add("dealt_card_count")
                if len(set(flattened)) != len(flattened):
                    deal_flags.add("duplicate_ownership")
                dealt_cards = set(flattened)
                if expected_cards - dealt_cards:
                    deal_flags.add("undealt_cards")
                if dealt_cards - expected_cards:
                    deal_flags.add("unexpected_dealt_cards")
                try:
                    validate_deal_invariants(hands, ordered_ids)
                except Exception:
                    deal_flags.add("deal_validator")
                attach_dealt_hands(audit, hands)
            except Exception:
                deal_flags.add("deal_failure")
            finally:
                deal_times.append(perf_counter_ns() - deal_started)

            if hands_list is not None and "deal_failure" not in deal_flags:
                verification_started = perf_counter_ns()
                try:
                    if not verify_shuffle_audit(audit):
                        commitment_verification_failures += 1
                except Exception:
                    commitment_verification_failures += 1
                finally:
                    verification_times.append(
                        perf_counter_ns() - verification_started
                    )

                for seat, hand in enumerate(hands_list):
                    for card in hand:
                        seat_card_frequency[str(seat)][_card_key(card)] += 1
                    for suit in Suit:
                        suit_counts[suit.value][
                            sum(card.suit == suit for card in hand)
                        ] += 1
                    red = sum(card.suit in RED_SUITS for card in hand)
                    mendis = sum(card.rank == Rank.TEN for card in hand)
                    red_counts[red] += 1
                    black_counts[len(hand) - red] += 1
                    mendis_per_hand[mendis] += 1

                for team_id, parity in (("TeamA", 0), ("TeamB", 1)):
                    team_mendis = sum(
                        card.rank == Rank.TEN
                        for seat, hand in enumerate(hands_list)
                        if seat % 2 == parity
                        for card in hand
                    )
                    mendis_per_team[team_id][team_mendis] += 1
                completed_deals += 1

            if deal_flags:
                invariant_failures["deals_with_any_failure"] += 1
                for flag in deal_flags:
                    invariant_failures[flag] += 1
        finally:
            total_times.append(perf_counter_ns() - total_started)

    failure_names = (
        "shuffle_failure",
        "shuffled_card_count",
        "shuffled_duplicates",
        "shuffled_missing_cards",
        "shuffled_unexpected_cards",
        "hand_size",
        "dealt_card_count",
        "duplicate_ownership",
        "undealt_cards",
        "unexpected_dealt_cards",
        "deal_validator",
        "deal_failure",
        "deals_with_any_failure",
    )
    return {
        "deals_requested": deals,
        "deals_completed": completed_deals,
        "player_count": player_count,
        "hand_size": hand_size,
        "card_position_frequency": position_frequency,
        "seat_card_frequency": seat_card_frequency,
        "suit_count_distribution_per_hand": {
            suit.value: _histogram(suit_counts[suit.value], hand_size)
            for suit in Suit
        },
        "red_black_distribution_per_hand": {
            "red": _histogram(red_counts, hand_size),
            "black": _histogram(black_counts, hand_size),
        },
        "mendis_per_hand": _histogram(mendis_per_hand, 4),
        "mendis_per_team": {
            team_id: _histogram(counter, 4)
            for team_id, counter in mendis_per_team.items()
        },
        "observed_extremes": {
            "suit_cards_per_hand": {
                suit.value: _observed_range(suit_counts[suit.value])
                for suit in Suit
            },
            "red_cards_per_hand": _observed_range(red_counts),
            "black_cards_per_hand": _observed_range(black_counts),
            "mendis_per_hand": _observed_range(mendis_per_hand),
        },
        "invariant_failures": {
            name: invariant_failures[name] for name in failure_names
        },
        "commitment_verification_failures": commitment_verification_failures,
        "timing_ms": {
            "shuffle_commit": _timing_summary(shuffle_times),
            "deal_validate": _timing_summary(deal_times),
            "audit_verify": _timing_summary(verification_times),
            "total": _timing_summary(total_times),
        },
    }


def run_simulation(
    deals: int,
    player_modes: Sequence[int] = PLAYER_COUNTS,
    *,
    service_factory: ServiceFactory = SecureShuffleService,
) -> dict[str, Any]:
    """Run ``deals`` secure deals for every requested player mode."""
    if deals <= 0:
        raise ValueError("deals must be positive")
    modes = tuple(dict.fromkeys(player_modes))
    if not modes or any(mode not in PLAYER_COUNTS for mode in modes):
        raise ValueError("player modes must contain only 4, 6, or 8")

    mode_reports = {
        str(mode): _simulate_mode(deals, mode, service_factory) for mode in modes
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "versions": {
            "deck_definition": DECK_DEFINITION_VERSION,
            "shuffle_algorithm": SHUFFLE_ALGORITHM_VERSION,
            "dealing_algorithm": DEALING_ALGORITHM_VERSION,
        },
        "deals_per_player_mode": deals,
        "player_modes": list(modes),
        "total_deals_requested": deals * len(modes),
        "total_deals_completed": sum(
            report["deals_completed"] for report in mode_reports.values()
        ),
        "modes": mode_reports,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate auditable, unbalanced secure MendiCot deals offline."
    )
    parser.add_argument("--deals", type=_positive_int, default=100_000)
    parser.add_argument(
        "--players",
        type=int,
        choices=PLAYER_COUNTS,
        nargs="+",
        default=list(PLAYER_COUNTS),
        help="player modes to simulate (default: 4 6 8)",
    )
    parser.add_argument("--json-output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_simulation(args.deals, args.players)

    print("MendiCot secure deal simulation")
    for mode in report["player_modes"]:
        result = report["modes"][str(mode)]
        failures = result["invariant_failures"]["deals_with_any_failure"]
        verification = result["commitment_verification_failures"]
        timing = result["timing_ms"]
        extremes = result["observed_extremes"]
        suit_extremes = ", ".join(
            f"{suit} {limits['min']}-{limits['max']}"
            for suit, limits in extremes["suit_cards_per_hand"].items()
        )
        print(
            f"{mode} players: {result['deals_completed']}/{result['deals_requested']} deals; "
            f"invariant failures={failures}; audit failures={verification}; "
            f"shuffle avg={timing['shuffle_commit']['average_ms']:.3f} ms; "
            f"deal avg={timing['deal_validate']['average_ms']:.3f} ms; "
            f"verify avg={timing['audit_verify']['average_ms']:.3f} ms; "
            f"total max={timing['total']['max_ms']:.3f} ms"
        )
        print(
            f"  observed natural hand ranges: {suit_extremes}; "
            f"red {extremes['red_cards_per_hand']['min']}-"
            f"{extremes['red_cards_per_hand']['max']}; "
            f"Mendis {extremes['mendis_per_hand']['min']}-"
            f"{extremes['mendis_per_hand']['max']}"
        )

    if args.json_output is not None:
        args.json_output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"JSON report: {args.json_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
