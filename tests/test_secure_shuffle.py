import copy
import hashlib
from collections import Counter, defaultdict
from dataclasses import replace

import pytest

from mendicot.deck import (
    CANONICAL_DECK,
    CANONICAL_RANK_ORDER,
    CANONICAL_SUIT_ORDER,
    DEALING_ALGORITHM_VERSION,
    DECK_DEFINITION_VERSION,
    create_deck,
    deal_cards,
    validate_deal_invariants,
    validate_deck_definition,
)
from mendicot.enums import Rank, Suit
from mendicot.exceptions import (
    DealInvariantFailed,
    InvalidDeckDefinition,
    ShuffleCommitmentFailed,
    ShuffleVerificationFailed,
)
from mendicot.models import Card, Player
from mendicot.secure_shuffle import (
    SHUFFLE_ALGORITHM_VERSION,
    SHUFFLE_CONTEXT_VERSION,
    DeterministicEntropySource,
    HmacSha256Random,
    SecretsEntropySource,
    SecureShuffleService,
    attach_dealt_hands,
    build_shuffle_context,
    canonical_json_bytes,
    commitment_hash,
    deck_hash,
    secure_fisher_yates,
    verify_played_card_ownership,
    verify_shuffle_audit,
)


def _players(player_count: int = 4, *, reverse: bool = False) -> list[Player]:
    players = [
        Player(
            player_id=f"P{seat_index}",
            team_id="TeamA" if seat_index % 2 == 0 else "TeamB",
            seat_index=seat_index,
        )
        for seat_index in range(player_count)
    ]
    return list(reversed(players)) if reverse else players


def _prepared(seed: bytes = b"secure-shuffle-test-seed", player_count: int = 4):
    service = SecureShuffleService(DeterministicEntropySource(seed))
    return service.prepare_shuffle(
        game_id="game-1",
        room_id="room-1",
        players=_players(player_count, reverse=True),
        trump_mode="hidden",
        selected_first_player_id="P2",
        selected_trump_hider_id="P1",
    )


def _attached_record(seed: bytes = b"secure-shuffle-test-seed"):
    shuffled, record = _prepared(seed)
    ordered_ids = [seat["player_id"] for seat in record.context["ordered_seats"]]
    dealt = deal_cards(shuffled, len(ordered_ids))
    hands = {
        player_id: dealt[index]
        for index, player_id in reversed(list(enumerate(ordered_ids)))
    }
    attach_dealt_hands(record, hands)
    return shuffled, record, ordered_ids, hands


def test_canonical_deck_is_exactly_the_versioned_mendicot_48_definition():
    assert DECK_DEFINITION_VERSION == "mendicot-48-v1"
    assert DEALING_ALGORITHM_VERSION == "round-robin-seat-order-v1"
    assert CANONICAL_SUIT_ORDER == (
        Suit.SPADES,
        Suit.HEARTS,
        Suit.DIAMONDS,
        Suit.CLUBS,
    )
    assert CANONICAL_RANK_ORDER == tuple(Rank)
    assert CANONICAL_DECK == tuple(
        Card(suit, rank)
        for suit in CANONICAL_SUIT_ORDER
        for rank in CANONICAL_RANK_ORDER
    )
    assert len(CANONICAL_DECK) == len(set(CANONICAL_DECK)) == 48
    assert all(card.rank.value != 2 for card in CANONICAL_DECK)
    validate_deck_definition()


@pytest.mark.parametrize(
    "invalid_deck",
    [
        list(CANONICAL_DECK[:-1]),
        list(CANONICAL_DECK) + [CANONICAL_DECK[0]],
        list(CANONICAL_DECK[:-1]) + [CANONICAL_DECK[0]],
        list(CANONICAL_DECK[:-1]) + [Card("JOKER", Rank.ACE)],
    ],
)
def test_deck_definition_rejects_missing_duplicate_and_unexpected_cards(invalid_deck):
    with pytest.raises(InvalidDeckDefinition):
        validate_deck_definition(invalid_deck)


def test_create_and_shuffle_never_mutate_the_canonical_or_input_deck():
    canonical_before = tuple(CANONICAL_DECK)
    fresh = create_deck()
    fresh.reverse()
    fresh.pop()
    assert CANONICAL_DECK == canonical_before
    assert create_deck() == list(canonical_before)

    source = create_deck()
    source_before = list(source)
    shuffled = secure_fisher_yates(source, HmacSha256Random(b"a" * 32))
    assert source == source_before
    assert shuffled is not source
    assert set(shuffled) == set(source)


class _RecordingRandBelow:
    def __init__(self, choose_last: bool):
        self.choose_last = choose_last
        self.bounds: list[int] = []

    def randbelow(self, upper_exclusive: int) -> int:
        self.bounds.append(upper_exclusive)
        return upper_exclusive - 1 if self.choose_last else 0


def test_fisher_yates_uses_each_correct_inclusive_swap_range():
    cards = create_deck()[:6]
    source = _RecordingRandBelow(choose_last=True)
    assert secure_fisher_yates(cards, source) == cards
    # randbelow(i + 1) permits every index from zero through i, inclusively.
    assert source.bounds == [6, 5, 4, 3, 2]


class _RejectThenAcceptRandom(HmacSha256Random):
    def __init__(self):
        super().__init__(b"r" * 32)
        self.samples = [b"\xff", b"\x0a"]

    def _bytes(self, length: int) -> bytes:
        assert length == 1
        return self.samples.pop(0)


def test_randbelow_rejection_sampling_avoids_modulo_bias():
    source = _RejectThenAcceptRandom()
    # For upper bound 10, 250..255 are rejected from the 256-value sample space.
    assert source.randbelow(10) == 0
    assert source.samples == []


def test_deterministic_entropy_and_shuffle_are_reproducible_but_seed_sensitive():
    first_deck, first_record = _prepared(b"same-seed")
    second_deck, second_record = _prepared(b"same-seed")
    different_deck, different_record = _prepared(b"different-seed")

    assert first_deck == second_deck
    assert first_record.commitment_hash == second_record.commitment_hash
    assert first_record.shuffled_deck_hash == second_record.shuffled_deck_hash
    assert first_deck != different_deck
    assert first_record.commitment_hash != different_record.commitment_hash


def test_deterministic_entropy_stream_is_independent_of_read_chunking():
    chunked = DeterministicEntropySource(b"chunk-seed")
    whole = DeterministicEntropySource(b"chunk-seed")
    assert chunked.token_bytes(7) + chunked.token_bytes(41) == whole.token_bytes(48)


def test_production_service_defaults_only_to_the_secure_entropy_source():
    service = SecureShuffleService()
    assert isinstance(service.entropy_source, SecretsEntropySource)
    assert service.entropy_source.is_production_secure is True
    assert DeterministicEntropySource(b"test-only").is_production_secure is False
    assert len(service.entropy_source.token_bytes(32)) == 32


class _ShortEntropy:
    def token_bytes(self, length: int) -> bytes:
        return b"x"


def test_bad_entropy_source_fails_as_a_stable_commitment_error():
    service = SecureShuffleService(_ShortEntropy())
    with pytest.raises(ShuffleCommitmentFailed):
        service.prepare_shuffle(
            game_id="game",
            room_id="room",
            players=_players(),
            trump_mode="normal",
        )


def test_canonical_json_and_deck_hash_are_stable_and_order_sensitive():
    assert canonical_json_bytes({"b": 2, "a": "♥"}) == (
        b'{"a":"\xe2\x99\xa5","b":2}'
    )
    canonical = create_deck()
    assert deck_hash(canonical) == (
        "4913915dcf980865748c618fa1e93f5d6ee38ac792a2e8a32db4b99deb910dd3"
    )
    assert deck_hash(canonical) == deck_hash(list(canonical))
    tampered = list(canonical)
    tampered[0], tampered[1] = tampered[1], tampered[0]
    assert deck_hash(tampered) != deck_hash(canonical)


def test_shuffle_context_canonically_sorts_and_versions_authoritative_seats():
    context = build_shuffle_context(
        game_id="game-1",
        room_id="room-1",
        players=_players(reverse=True),
        trump_mode="normal",
        nonce_hex="ab" * 16,
        selected_first_player_id="P2",
        selected_trump_hider_id=None,
    )
    assert context["context_version"] == SHUFFLE_CONTEXT_VERSION
    assert context["deck_definition_version"] == DECK_DEFINITION_VERSION
    assert context["shuffle_algorithm_version"] == SHUFFLE_ALGORITHM_VERSION
    assert context["dealing_algorithm_version"] == DEALING_ALGORITHM_VERSION
    assert context["player_count"] == 4
    assert [seat["seat_index"] for seat in context["ordered_seats"]] == [0, 1, 2, 3]
    assert [seat["player_id"] for seat in context["ordered_seats"]] == [
        "P0",
        "P1",
        "P2",
        "P3",
    ]
    reordered = {key: context[key] for key in reversed(tuple(context))}
    assert commitment_hash(b"s" * 32, reordered) == commitment_hash(
        b"s" * 32, context
    )


def test_valid_commitment_reveal_reproduces_deck_and_round_robin_ownership():
    shuffled, record, _, _ = _attached_record()
    assert verify_shuffle_audit(
        record,
        revealed_server_secret=record.server_secret,
        nonce=record.nonce,
        context=copy.deepcopy(record.context),
        shuffled_deck=list(shuffled),
    ) is True

    private = record.public_metadata()
    assert "revealed_server_secret" not in private
    assert "nonce" not in private
    assert "shuffled_deck" not in private
    assert "shuffled_deck_hash" not in private
    with pytest.raises(ShuffleVerificationFailed):
        record.public_metadata(reveal=True)
    record.audit_status = "VERIFIED"
    revealed = record.public_metadata(reveal=True)
    assert revealed["revealed_server_secret"] == record.server_secret.hex()
    assert revealed["nonce"] == record.nonce.hex()
    assert revealed["shuffled_deck_hash"] == deck_hash(shuffled)


def test_reveal_verification_rejects_tampered_secret_nonce_context_and_deck():
    shuffled, record, _, _ = _attached_record()
    bad_secret = bytes([record.server_secret[0] ^ 1]) + record.server_secret[1:]
    bad_context = copy.deepcopy(record.context)
    bad_context["game_id"] = "different-game"
    bad_deck = list(shuffled)
    bad_deck[0], bad_deck[1] = bad_deck[1], bad_deck[0]

    for overrides in (
        {"revealed_server_secret": bad_secret},
        {"nonce": bytes([record.nonce[0] ^ 1]) + record.nonce[1:]},
        {"context": bad_context},
        {"shuffled_deck": bad_deck},
    ):
        with pytest.raises(ShuffleVerificationFailed):
            verify_shuffle_audit(record, **overrides)


def test_verification_rejects_tampered_record_hash_and_dealt_ownership():
    _, record, ordered_ids, _ = _attached_record()
    bad_hash_record = replace(record, shuffled_deck_hash="0" * 64)
    with pytest.raises(ShuffleVerificationFailed):
        verify_shuffle_audit(bad_hash_record)

    tampered_hands = dict(record.dealt_hands)
    first = list(tampered_hands[ordered_ids[0]])
    second = list(tampered_hands[ordered_ids[1]])
    first[0], second[0] = second[0], first[0]
    tampered_hands[ordered_ids[0]] = tuple(first)
    tampered_hands[ordered_ids[1]] = tuple(second)
    ownership_record = replace(record, dealt_hands=tampered_hands)
    with pytest.raises(ShuffleVerificationFailed):
        verify_shuffle_audit(ownership_record)


@pytest.mark.parametrize("player_count,hand_size", [(4, 12), (6, 8), (8, 6)])
def test_round_robin_deal_uses_seat_order_and_partitions_all_48_cards(
    player_count, hand_size
):
    deck = create_deck()
    hands = deal_cards(deck, player_count)
    assert len(hands) == player_count
    assert all(len(hand) == hand_size for hand in hands)
    assert all(hands[seat] == deck[seat::player_count] for seat in range(player_count))
    assert [
        hands[card_index % player_count][card_index // player_count]
        for card_index in range(48)
    ] == deck
    all_cards = [card for hand in hands for card in hand]
    assert len(all_cards) == len(set(all_cards)) == 48


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_deal_invariants_accept_mapping_order_independent_authoritative_hands(player_count):
    ordered_ids = [f"P{index}" for index in range(player_count)]
    dealt = deal_cards(create_deck(), player_count)
    hands = {
        ordered_ids[index]: dealt[index]
        for index in reversed(range(player_count))
    }
    validate_deal_invariants(hands, ordered_ids)


def test_deal_invariants_reject_invalid_players_owners_sizes_and_cards():
    ordered_ids = ["P0", "P1", "P2", "P3"]
    dealt = deal_cards(create_deck(), 4)
    valid = {player_id: list(dealt[index]) for index, player_id in enumerate(ordered_ids)}

    cases = []
    cases.append((valid, ["P0", "P0", "P2", "P3"]))
    cases.append(({key: value for key, value in valid.items() if key != "P3"}, ordered_ids))
    wrong_size = copy.deepcopy(valid)
    wrong_size["P0"].pop()
    cases.append((wrong_size, ordered_ids))
    duplicate_owner = copy.deepcopy(valid)
    duplicate_owner["P1"][0] = duplicate_owner["P0"][0]
    cases.append((duplicate_owner, ordered_ids))
    foreign_card = copy.deepcopy(valid)
    foreign_card["P0"][0] = Card("JOKER", Rank.ACE)
    cases.append((foreign_card, ordered_ids))

    for hands, players in cases:
        with pytest.raises(DealInvariantFailed):
            validate_deal_invariants(hands, players)


def test_many_deterministic_shuffles_have_broad_position_and_seat_variation():
    canonical = create_deck()
    trials = 256
    positions: dict[Card, set[int]] = defaultdict(set)
    position_counts: dict[Card, Counter[int]] = defaultdict(Counter)
    seats: dict[Card, set[int]] = defaultdict(set)
    first_cards: Counter[Card] = Counter()
    last_cards: Counter[Card] = Counter()
    deck_hashes: set[str] = set()
    suit_profiles: set[tuple[int, ...]] = set()
    red_counts: set[int] = set()

    for trial in range(trials):
        seed = hashlib.sha256(f"shuffle-trial-{trial}".encode()).digest()
        shuffled = secure_fisher_yates(canonical, HmacSha256Random(seed))
        validate_deck_definition(shuffled)
        deck_hashes.add(deck_hash(shuffled))
        first_cards[shuffled[0]] += 1
        last_cards[shuffled[-1]] += 1
        for position, card in enumerate(shuffled):
            positions[card].add(position)
            position_counts[card][position] += 1
            seats[card].add(position % 4)
        for hand in deal_cards(shuffled, 4):
            profile = tuple(sum(card.suit == suit for card in hand) for suit in Suit)
            suit_profiles.add(profile)
            red_counts.add(
                sum(card.suit in (Suit.HEARTS, Suit.DIAMONDS) for card in hand)
            )

    assert len(deck_hashes) == trials
    assert min(len(card_positions) for card_positions in positions.values()) >= 30
    assert max(max(counts.values()) for counts in position_counts.values()) < 20
    assert all(card_seats == {0, 1, 2, 3} for card_seats in seats.values())
    assert len(first_cards) >= 40 and max(first_cards.values()) < 20
    assert len(last_cards) >= 40 and max(last_cards.values()) < 20
    assert len(suit_profiles) >= 50
    assert min(red_counts) < 6 < max(red_counts)


def test_empty_or_mutated_context_cannot_bypass_commitment_verification():
    _, record, _, _ = _attached_record()
    with pytest.raises(ShuffleVerificationFailed):
        verify_shuffle_audit(record, context={})

    record.context["game_id"] = "mutated-after-commit"
    with pytest.raises(ShuffleVerificationFailed):
        verify_shuffle_audit(record)


def test_revealed_metadata_is_a_copy_of_the_authoritative_context():
    _, record, _, _ = _attached_record()
    assert verify_shuffle_audit(record) is True
    record.audit_status = "VERIFIED"
    revealed = record.public_metadata(reveal=True)
    revealed["canonical_context"]["game_id"] = "caller-mutation"
    assert record.context["game_id"] == "game-1"
    assert verify_shuffle_audit(record) is True


def test_play_history_verifier_rejects_wrong_owner_and_duplicate_card():
    _, record, ordered_ids, _ = _attached_record()
    first_player, second_player = ordered_ids[:2]
    first_card = record.dealt_hands[first_player][0]
    second_card = record.dealt_hands[second_player][0]

    assert verify_played_card_ownership(
        record,
        [(first_player, first_card), (second_player, second_card)],
    ) is True
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(record, [(second_player, first_card)])
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(
            record,
            [(first_player, first_card), (first_player, first_card)],
        )


def test_shuffle_service_rejects_invalid_context_before_committing():
    service = SecureShuffleService(DeterministicEntropySource(b"bad-context"))
    with pytest.raises(ShuffleCommitmentFailed):
        service.prepare_shuffle(
            game_id="game",
            room_id="room",
            players=_players(),
            trump_mode="bogus",
        )
    with pytest.raises(ShuffleCommitmentFailed):
        service.prepare_shuffle(
            game_id="game",
            room_id="room",
            players=_players(),
            trump_mode="hidden",
            selected_trump_hider_id=None,
        )


def test_complete_play_history_verifies_all_48_cards_with_correct_owners():
    _, record, ordered_ids, hands = _attached_record()
    # Build a valid complete play history (all cards played by their owner)
    played_cards = [
        (player_id, card)
        for player_id in ordered_ids
        for card in hands[player_id]
    ]
    assert len(played_cards) == 48
    assert verify_played_card_ownership(
        record, played_cards, require_complete=True,
    ) is True


def test_complete_verification_rejects_wrong_owner():
    _, record, ordered_ids, hands = _attached_record()
    played_cards = [
        (player_id, card)
        for player_id in ordered_ids
        for card in hands[player_id]
    ]
    # Swap owner on first card
    wrong_pid = ordered_ids[1]
    played_cards[0] = (wrong_pid, played_cards[0][1])
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(
            record, played_cards, require_complete=True,
        )


def test_complete_verification_rejects_fabricated_card():
    _, record, ordered_ids, hands = _attached_record()
    played_cards = [
        (player_id, card)
        for player_id in ordered_ids
        for card in hands[player_id]
    ]
    fake_card = Card("JOKER", 99)
    played_cards[0] = (ordered_ids[0], fake_card)
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(
            record, played_cards, require_complete=True,
        )


def test_complete_verification_rejects_duplicate_card():
    _, record, ordered_ids, hands = _attached_record()
    played_cards = [
        (player_id, card)
        for player_id in ordered_ids
        for card in hands[player_id]
    ]
    # Duplicate the first card in place of the last card
    played_cards[-1] = played_cards[0]
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(
            record, played_cards, require_complete=True,
        )


def test_complete_verification_rejects_missing_cards():
    _, record, ordered_ids, hands = _attached_record()
    played_cards = [
        (player_id, card)
        for player_id in ordered_ids
        for card in hands[player_id]
    ]
    # Remove last card (47 instead of 48)
    played_cards.pop()
    with pytest.raises(ShuffleVerificationFailed):
        verify_played_card_ownership(
            record, played_cards, require_complete=True,
        )