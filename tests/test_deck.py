import random
from mendicot.deck import create_deck, shuffle_deck
from mendicot.enums import Suit, Rank

def test_deck_has_48_cards():
    deck = create_deck()
    assert len(deck) == 48

def test_no_twos_in_deck():
    deck = create_deck()
    ranks = [card.rank.value for card in deck]
    assert 2 not in ranks

def test_all_suits_present():
    deck = create_deck()
    suits = {card.suit for card in deck}
    assert suits == set(Suit)

def test_all_valid_ranks_present():
    deck = create_deck()
    ranks = {card.rank for card in deck}
    expected_ranks = set(Rank)
    assert ranks == expected_ranks

def test_no_duplicate_cards():
    deck = create_deck()
    unique_cards = set(deck)
    assert len(unique_cards) == 48

def test_shuffle_is_deterministic_with_rng():
    deck1 = create_deck()
    deck2 = create_deck()
    
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    
    shuffled1 = shuffle_deck(deck1, rng1)
    shuffled2 = shuffle_deck(deck2, rng2)
    
    assert shuffled1 == shuffled2

def test_shuffle_produces_different_order():
    deck = create_deck()
    rng = random.Random(42)
    shuffled = shuffle_deck(list(deck), rng)
    
    assert deck != shuffled
