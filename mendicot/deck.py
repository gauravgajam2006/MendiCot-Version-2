import random
from .enums import Suit, Rank
from .models import Card

def create_deck() -> list[Card]:
    """Create a standard deck of 48 cards (ranks 3 to Ace for all suits)."""
    deck = []
    for suit in Suit:
        for rank in Rank:
            deck.append(Card(suit, rank))
    # Sort for deterministic initial state before shuffle
    deck.sort(key=lambda c: (c.suit.value, c.rank.value))
    return deck

def shuffle_deck(deck: list[Card], rng: random.Random | None = None) -> list[Card]:
    """Shuffle the deck using provided RNG or SystemRandom."""
    shuffled = list(deck)
    if rng is None:
        rng = random.SystemRandom()
    rng.shuffle(shuffled)
    return shuffled

def deal_cards(deck: list[Card], player_count: int) -> list[list[Card]]:
    """Deal cards equally among players."""
    if player_count not in (4, 6, 8):
        from .exceptions import InvalidPlayerCount
        raise InvalidPlayerCount()
    
    cards_per_player = len(deck) // player_count
    hands = []
    for i in range(player_count):
        start = i * cards_per_player
        end = start + cards_per_player
        hands.append(deck[start:end])
    return hands
