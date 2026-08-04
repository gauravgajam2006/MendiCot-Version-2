"""Cryptographically secure, reproducible shuffle and deal audit primitives.

Production entropy comes exclusively from secrets.token_bytes. The secret is
committed before use, HMAC-SHA256 derives a deterministic shuffle seed, and
rejection-sampled Fisher-Yates avoids modulo bias. Deterministic entropy is
injectable only through Python constructors for tests and developer tools; no
HTTP, WebSocket, or environment switch selects it.

Audit records and active games follow the repository's current in-memory
architecture. A process restart loses both; this module does not claim restart
recovery or introduce a partial persistence layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Protocol, Sequence

from .deck import (
    DEALING_ALGORITHM_VERSION,
    DECK_DEFINITION_VERSION,
    create_deck,
    deal_cards,
    validate_deal_invariants,
)
from .exceptions import (
    MendiCotError,
    ShuffleCommitmentFailed,
    ShuffleVerificationFailed,
)
from .models import Card, Player
from .validators import (
    validate_player_count,
    validate_seating,
    validate_team_configuration,
)


SHUFFLE_ALGORITHM_VERSION = "hmac-sha256-fisher-yates-v1"
SHUFFLE_CONTEXT_VERSION = "mendicot-shuffle-context-v1"
SUPPORTED_TRUMP_MODES = frozenset(("normal", "hidden"))


class EntropySource(Protocol):
    """Narrow entropy interface used only while preparing a new deal."""

    def token_bytes(self, length: int) -> bytes: ...


class SecretsEntropySource:
    """Production source backed by operating-system cryptographic entropy."""

    is_production_secure = True

    def token_bytes(self, length: int) -> bytes:
        return secrets.token_bytes(length)


class DeterministicEntropySource:
    """Explicit test/tool source; never selected through a public game API."""

    is_production_secure = False

    def __init__(self, seed: bytes):
        if not seed:
            raise ValueError("Deterministic entropy seed cannot be empty.")
        self._seed = bytes(seed)
        self._counter = 0
        self._buffer = bytearray()

    def token_bytes(self, length: int) -> bytes:
        if length < 0:
            raise ValueError("Entropy length cannot be negative.")
        while len(self._buffer) < length:
            block = hmac.new(
                self._seed,
                self._counter.to_bytes(16, "big"),
                hashlib.sha256,
            ).digest()
            self._counter += 1
            self._buffer.extend(block)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result


class HmacSha256Random:
    """Deterministic cryptographic byte/index generator for audit replay."""

    def __init__(self, seed: bytes):
        if len(seed) < 32:
            raise ValueError("Shuffle seed must contain at least 256 bits.")
        self._seed = bytes(seed)
        self._counter = 0
        self._buffer = bytearray()

    def _bytes(self, length: int) -> bytes:
        while len(self._buffer) < length:
            self._buffer.extend(
                hmac.new(
                    self._seed,
                    b"mendicot-rng-v1\x00" + self._counter.to_bytes(16, "big"),
                    hashlib.sha256,
                ).digest()
            )
            self._counter += 1
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result

    def randbelow(self, upper_exclusive: int) -> int:
        """Return an unbiased integer below the exclusive upper bound."""
        if upper_exclusive <= 0:
            raise ValueError("Upper bound must be positive.")
        byte_count = max(1, (upper_exclusive.bit_length() + 7) // 8)
        sample_space = 1 << (byte_count * 8)
        accepted_limit = sample_space - (sample_space % upper_exclusive)
        while True:
            candidate = int.from_bytes(self._bytes(byte_count), "big")
            if candidate < accepted_limit:
                return candidate % upper_exclusive


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def serialize_card(card: Card) -> dict[str, object]:
    return {"rank": card.rank.value, "suit": card.suit.value}


def deck_hash(deck: Sequence[Card]) -> str:
    return hashlib.sha256(
        canonical_json_bytes([serialize_card(card) for card in deck])
    ).hexdigest()


def secure_fisher_yates(
    deck: Sequence[Card],
    random_source: HmacSha256Random | None = None,
) -> list[Card]:
    """Shuffle a fresh copy with inclusive Fisher-Yates index bounds."""
    shuffled = list(deck)
    if random_source is None:
        random_source = HmacSha256Random(secrets.token_bytes(32))
    for index in range(len(shuffled) - 1, 0, -1):
        swap_index = random_source.randbelow(index + 1)
        shuffled[index], shuffled[swap_index] = (
            shuffled[swap_index],
            shuffled[index],
        )
    return shuffled


def build_shuffle_context(
    *,
    game_id: str,
    room_id: str,
    players: Sequence[Player],
    trump_mode: str,
    nonce_hex: str,
    selected_first_player_id: str | None = None,
    selected_trump_hider_id: str | None = None,
) -> dict[str, object]:
    validate_player_count(len(players))
    validate_team_configuration(list(players))
    validate_seating(list(players))
    if not isinstance(game_id, str) or not game_id:
        raise ShuffleCommitmentFailed()
    if not isinstance(room_id, str) or not room_id:
        raise ShuffleCommitmentFailed()
    if trump_mode not in SUPPORTED_TRUMP_MODES:
        raise ShuffleCommitmentFailed()
    try:
        nonce = bytes.fromhex(nonce_hex)
    except (TypeError, ValueError) as exc:
        raise ShuffleCommitmentFailed() from exc
    if len(nonce) != 16 or nonce.hex() != nonce_hex.lower():
        raise ShuffleCommitmentFailed()

    ordered_players = sorted(players, key=lambda player: player.seat_index)
    player_ids = {player.player_id for player in ordered_players}
    if (
        selected_first_player_id is not None
        and selected_first_player_id not in player_ids
    ):
        raise ShuffleCommitmentFailed()
    if (
        selected_trump_hider_id is not None
        and selected_trump_hider_id not in player_ids
    ):
        raise ShuffleCommitmentFailed()
    if trump_mode == "hidden" and selected_trump_hider_id is None:
        raise ShuffleCommitmentFailed()
    if trump_mode == "normal" and selected_trump_hider_id is not None:
        raise ShuffleCommitmentFailed()

    return {
        "context_version": SHUFFLE_CONTEXT_VERSION,
        "game_id": game_id,
        "room_id": room_id,
        "player_count": len(ordered_players),
        "ordered_seats": [
            {
                "player_id": player.player_id,
                "seat_index": player.seat_index,
                "team_id": player.team_id,
            }
            for player in ordered_players
        ],
        "trump_mode": trump_mode,
        "selected_first_player_id": selected_first_player_id,
        "selected_trump_hider_id": selected_trump_hider_id,
        "nonce": nonce_hex,
        "deck_definition_version": DECK_DEFINITION_VERSION,
        "shuffle_algorithm_version": SHUFFLE_ALGORITHM_VERSION,
        "dealing_algorithm_version": DEALING_ALGORITHM_VERSION,
        "player_entropy": [],
    }


def commitment_hash(server_secret: bytes, context: dict[str, object]) -> str:
    payload = {
        "context": context,
        "server_secret": server_secret.hex(),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def derive_shuffle_seed(
    server_secret: bytes,
    canonical_context: bytes,
) -> bytes:
    return hmac.new(server_secret, canonical_context, hashlib.sha256).digest()


@dataclass
class ShuffleAuditRecord:
    game_id: str
    room_id: str
    server_secret: bytes
    nonce: bytes
    context: dict[str, object]
    canonical_context: bytes
    commitment_hash: str
    shuffled_deck_hash: str
    shuffled_deck: tuple[Card, ...]
    deal_timestamp: str
    dealt_hands: dict[str, tuple[Card, ...]] = field(default_factory=dict)
    audit_status: str = "COMMITTED"

    def public_metadata(self, reveal: bool = False) -> dict[str, object]:
        metadata: dict[str, object] = {
            "commitment_hash": self.commitment_hash,
            "deck_definition_version": self.context[
                "deck_definition_version"
            ],
            "shuffle_algorithm_version": self.context[
                "shuffle_algorithm_version"
            ],
            "dealing_algorithm_version": self.context[
                "dealing_algorithm_version"
            ],
            "audit_status": self.audit_status,
        }
        if reveal:
            if self.audit_status != "VERIFIED":
                raise ShuffleVerificationFailed()
            metadata.update(
                {
                    "revealed_server_secret": self.server_secret.hex(),
                    "nonce": self.nonce.hex(),
                    "canonical_context": deepcopy(self.context),
                    "shuffled_deck_hash": self.shuffled_deck_hash,
                    "shuffled_deck": [
                        serialize_card(card) for card in self.shuffled_deck
                    ],
                    "deal_timestamp": self.deal_timestamp,
                }
            )
        return metadata


class SecureShuffleService:
    def __init__(self, entropy_source: EntropySource | None = None):
        self.entropy_source = entropy_source or SecretsEntropySource()

    def prepare_shuffle(
        self,
        *,
        game_id: str,
        room_id: str,
        players: Sequence[Player],
        trump_mode: str,
        selected_first_player_id: str | None = None,
        selected_trump_hider_id: str | None = None,
    ) -> tuple[list[Card], ShuffleAuditRecord]:
        try:
            server_secret = self.entropy_source.token_bytes(32)
            nonce = self.entropy_source.token_bytes(16)
            if len(server_secret) != 32 or len(nonce) != 16:
                raise ValueError("Entropy source returned an invalid length.")
            context = build_shuffle_context(
                game_id=game_id,
                room_id=room_id,
                players=players,
                trump_mode=trump_mode,
                nonce_hex=nonce.hex(),
                selected_first_player_id=selected_first_player_id,
                selected_trump_hider_id=selected_trump_hider_id,
            )
            canonical_context = canonical_json_bytes(context)
            commitment = commitment_hash(server_secret, context)
            seed = derive_shuffle_seed(server_secret, canonical_context)
            shuffled = secure_fisher_yates(
                create_deck(),
                random_source=HmacSha256Random(seed),
            )
            record = ShuffleAuditRecord(
                game_id=game_id,
                room_id=room_id,
                server_secret=server_secret,
                nonce=nonce,
                context=deepcopy(context),
                canonical_context=canonical_context,
                commitment_hash=commitment,
                shuffled_deck_hash=deck_hash(shuffled),
                shuffled_deck=tuple(shuffled),
                deal_timestamp=datetime.now(timezone.utc).isoformat(),
            )
            return shuffled, record
        except MendiCotError:
            raise
        except Exception as exc:
            raise ShuffleCommitmentFailed() from exc


def attach_dealt_hands(
    record: ShuffleAuditRecord,
    hands: Mapping[str, Sequence[Card]],
) -> None:
    """Validate and atomically attach the authoritative ownership partition."""
    ordered_ids = _validate_replay_context(record.context)
    candidate_hands = {
        player_id: tuple(hand) for player_id, hand in hands.items()
    }
    validate_deal_invariants(
        {player_id: list(hand) for player_id, hand in candidate_hands.items()},
        ordered_ids,
    )
    if record.dealt_hands:
        if record.dealt_hands == candidate_hands:
            return
        raise ShuffleVerificationFailed()
    record.dealt_hands = candidate_hands


def _validate_replay_context(context: Mapping[str, object]) -> list[str]:
    """Validate the complete v1 context and return authoritative seat IDs."""
    try:
        if context["context_version"] != SHUFFLE_CONTEXT_VERSION:
            raise ShuffleVerificationFailed()
        if context["deck_definition_version"] != DECK_DEFINITION_VERSION:
            raise ShuffleVerificationFailed()
        if context["shuffle_algorithm_version"] != SHUFFLE_ALGORITHM_VERSION:
            raise ShuffleVerificationFailed()
        if context["dealing_algorithm_version"] != DEALING_ALGORITHM_VERSION:
            raise ShuffleVerificationFailed()
        if context["trump_mode"] not in SUPPORTED_TRUMP_MODES:
            raise ShuffleVerificationFailed()
        if not isinstance(context["game_id"], str) or not context["game_id"]:
            raise ShuffleVerificationFailed()
        if not isinstance(context["room_id"], str) or not context["room_id"]:
            raise ShuffleVerificationFailed()

        nonce_hex = context["nonce"]
        if not isinstance(nonce_hex, str):
            raise ShuffleVerificationFailed()
        nonce = bytes.fromhex(nonce_hex)
        if len(nonce) != 16 or nonce.hex() != nonce_hex.lower():
            raise ShuffleVerificationFailed()

        raw_seats = context["ordered_seats"]
        if not isinstance(raw_seats, list):
            raise ShuffleVerificationFailed()
        players = [
            Player(
                player_id=seat["player_id"],
                team_id=seat["team_id"],
                seat_index=seat["seat_index"],
            )
            for seat in raw_seats
            if isinstance(seat, dict)
        ]
        if len(players) != len(raw_seats):
            raise ShuffleVerificationFailed()
        validate_player_count(len(players))
        validate_team_configuration(players)
        validate_seating(players)
        if context["player_count"] != len(players):
            raise ShuffleVerificationFailed()

        ordered_players = sorted(players, key=lambda player: player.seat_index)
        ordered_ids = [player.player_id for player in ordered_players]
        for selected_key in (
            "selected_first_player_id",
            "selected_trump_hider_id",
        ):
            selected_id = context[selected_key]
            if selected_id is not None and selected_id not in ordered_ids:
                raise ShuffleVerificationFailed()
        if (
            context["trump_mode"] == "hidden"
            and context["selected_trump_hider_id"] is None
        ):
            raise ShuffleVerificationFailed()
        if (
            context["trump_mode"] == "normal"
            and context["selected_trump_hider_id"] is not None
        ):
            raise ShuffleVerificationFailed()
        if context["player_entropy"] != []:
            raise ShuffleVerificationFailed()
        return ordered_ids
    except ShuffleVerificationFailed:
        raise
    except Exception as exc:
        raise ShuffleVerificationFailed() from exc


def verify_played_card_ownership(
    record: ShuffleAuditRecord,
    played_cards: Sequence[tuple[str, Card]],
    *,
    require_complete: bool = False,
) -> bool:
    """Verify public play history against the committed initial hands."""
    if not record.dealt_hands:
        raise ShuffleVerificationFailed()
    seen_cards: set[Card] = set()
    try:
        for player_id, card in played_cards:
            if player_id not in record.dealt_hands:
                raise ShuffleVerificationFailed()
            if card not in record.dealt_hands[player_id] or card in seen_cards:
                raise ShuffleVerificationFailed()
            seen_cards.add(card)
        if require_complete:
            dealt_cards = {
                card
                for hand in record.dealt_hands.values()
                for card in hand
            }
            if len(played_cards) != 48 or seen_cards != dealt_cards:
                raise ShuffleVerificationFailed()
        return True
    except ShuffleVerificationFailed:
        raise
    except Exception as exc:
        raise ShuffleVerificationFailed() from exc


def verify_shuffle_audit(
    record: ShuffleAuditRecord,
    *,
    revealed_server_secret: bytes | None = None,
    nonce: bytes | None = None,
    context: dict[str, object] | None = None,
    shuffled_deck: Sequence[Card] | None = None,
) -> bool:
    """Reproduce commitment, shuffle, deck hash, and round-robin ownership."""
    try:
        if record.audit_status not in ("COMMITTED", "VERIFIED"):
            raise ShuffleVerificationFailed()
        if record.game_id != record.context.get("game_id"):
            raise ShuffleVerificationFailed()
        if record.room_id != record.context.get("room_id"):
            raise ShuffleVerificationFailed()
        if canonical_json_bytes(record.context) != record.canonical_context:
            raise ShuffleVerificationFailed()
        _validate_replay_context(record.context)

        secret = (
            record.server_secret
            if revealed_server_secret is None
            else revealed_server_secret
        )
        candidate_context = deepcopy(
            record.context if context is None else context
        )
        candidate_nonce = record.nonce if nonce is None else nonce
        if not isinstance(candidate_nonce, bytes) or len(candidate_nonce) != 16:
            raise ShuffleVerificationFailed()
        candidate_context["nonce"] = candidate_nonce.hex()
        ordered_ids = _validate_replay_context(candidate_context)
        canonical_context = canonical_json_bytes(candidate_context)
        if commitment_hash(secret, candidate_context) != record.commitment_hash:
            raise ShuffleVerificationFailed()
        seed = derive_shuffle_seed(secret, canonical_context)
        reproduced = secure_fisher_yates(
            create_deck(),
            random_source=HmacSha256Random(seed),
        )
        candidate_deck = list(
            record.shuffled_deck if shuffled_deck is None else shuffled_deck
        )
        if reproduced != candidate_deck:
            raise ShuffleVerificationFailed()
        if deck_hash(candidate_deck) != record.shuffled_deck_hash:
            raise ShuffleVerificationFailed()

        if not record.dealt_hands:
            raise ShuffleVerificationFailed()
        validate_deal_invariants(
            {
                player_id: list(hand)
                for player_id, hand in record.dealt_hands.items()
            },
            ordered_ids,
        )
        reproduced_hands = deal_cards(candidate_deck, len(ordered_ids))
        expected = {
            player_id: tuple(reproduced_hands[index])
            for index, player_id in enumerate(ordered_ids)
        }
        if expected != record.dealt_hands:
            raise ShuffleVerificationFailed()
        return True
    except ShuffleVerificationFailed:
        raise
    except Exception as exc:
        raise ShuffleVerificationFailed() from exc