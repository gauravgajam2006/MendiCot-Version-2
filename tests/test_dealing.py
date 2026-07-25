import pytest
from mendicot.deck import create_deck, deal_cards
from mendicot.exceptions import InvalidPlayerCount

def test_4_players_get_12_cards():
    deck = create_deck()
    hands = deal_cards(deck, 4)
    assert len(hands) == 4
    for hand in hands:
        assert len(hand) == 12

def test_6_players_get_8_cards():
    deck = create_deck()
    hands = deal_cards(deck, 6)
    assert len(hands) == 6
    for hand in hands:
        assert len(hand) == 8

def test_8_players_get_6_cards():
    deck = create_deck()
    hands = deal_cards(deck, 8)
    assert len(hands) == 8
    for hand in hands:
        assert len(hand) == 6

def test_no_duplicate_cards_after_deal():
    deck = create_deck()
    hands = deal_cards(deck, 4)
    all_dealt_cards = []
    for hand in hands:
        all_dealt_cards.extend(hand)
    assert len(all_dealt_cards) == 48
    assert len(set(all_dealt_cards)) == 48

def test_all_48_cards_distributed():
    deck = create_deck()
    hands = deal_cards(deck, 4)
    all_dealt_cards = set()
    for hand in hands:
        all_dealt_cards.update(hand)
    assert all_dealt_cards == set(deck)

@pytest.mark.parametrize("invalid_count", [3, 5, 7, 9, 2])
def test_invalid_player_count_rejected(invalid_count):
    deck = create_deck()
    with pytest.raises(InvalidPlayerCount):
        deal_cards(deck, invalid_count)
