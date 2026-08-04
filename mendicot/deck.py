"""Canonical MendiCot deck definition and round-robin dealing primitives."""

from collections.abc import Sequence

from .enums import Rank, Suit
from .exceptions import (
    DealInvariantFailed,
    InvalidDeckDefinition,
    InvalidPlayerCount,
)
from .models import Card


DECK_DEFINITION_VERSION = "mendicot-48-v1"
DEALING_ALGORITHM_VERSION = "round-robin-seat-order-v1"
CANONICAL_SUIT_ORDER = (
    Suit.SPADES,
    Suit.HEARTS,
    Suit.DIAMONDS,
    Suit.CLUBS,
)
CANONICAL_RANK_ORDER = tuple(Rank)
CANONICAL_DECK = tuple(
    Card(suit=suit, rank=rank)
    for suit in CANONICAL_SUIT_ORDER
    for rank in CANONICAL_RANK_ORDER
)


def validate_deck_definition(deck: Sequence[Card] = CANONICAL_DECK) -> None:
    """Validate an exact permutation of the versioned 48-card definition."""
    expected = set(CANONICAL_DECK)
    try:
        actual = set(deck)
    except TypeError as exc:
        raise InvalidDeckDefinition() from exc
    if (
        len(CANONICAL_DECK) != 48
        or len(expected) != 48
        or len(deck) != 48
        or len(actual) != 48
        or actual != expected
        or any(card.rank.value == 2 for card in deck)
    ):
        raise InvalidDeckDefinition()


def create_deck() -> list[Card]:
    """Return a fresh copy in deterministic suit/rank order."""
    validate_deck_definition(CANONICAL_DECK)
    return list(CANONICAL_DECK)


def deal_cards(deck: Sequence[Card], player_count: int) -> list[list[Card]]:
    """Deal all 48 cards one at a time in authoritative seat order."""
    if player_count not in (4, 6, 8):
        raise InvalidPlayerCount()
    validate_deck_definition(deck)

    hands = [[] for _ in range(player_count)]
    for card_index, card in enumerate(deck):
        hands[card_index % player_count].append(card)
    return hands


def validate_deal_invariants(
    hands: dict[str, list[Card]],
    ordered_player_ids: Sequence[str],
) -> None:
    """Validate the complete ownership partition before committing live state."""
    player_count = len(ordered_player_ids)
    if player_count not in (4, 6, 8):
        raise DealInvariantFailed()
    if len(set(ordered_player_ids)) != player_count:
        raise DealInvariantFailed()
    if set(hands) != set(ordered_player_ids):
        raise DealInvariantFailed()

    expected_hand_size = 48 // player_count
    if any(
        len(hands[player_id]) != expected_hand_size
        for player_id in ordered_player_ids
    ):
        raise DealInvariantFailed()

    all_cards = [
        card
        for player_id in ordered_player_ids
        for card in hands[player_id]
    ]
    try:
        validate_deck_definition(all_cards)
    except InvalidDeckDefinition as exc:
        raise DealInvariantFailed() from exc


# Backward-compatible import surface. The secure implementation remains
# centralized in secure_shuffle.py.
def shuffle_deck(deck, random_source=None):
    from .secure_shuffle import secure_fisher_yates

    return secure_fisher_yates(deck, random_source=random_source)