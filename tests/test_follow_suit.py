import pytest
from mendicot.models import Card, TrumpState
from mendicot.enums import Suit, Rank, TrumpStatus
from mendicot.validators import validate_follow_suit
from mendicot.exceptions import MustFollowSuit, MustPlayTrump

def test_must_follow_lead_suit():
    hand = [Card(Suit.HEARTS, Rank.FOUR), Card(Suit.SPADES, Rank.FIVE)]
    trump_state = TrumpState(status=TrumpStatus.NONE)
    with pytest.raises(MustFollowSuit):
        validate_follow_suit(hand, Suit.HEARTS, Card(Suit.SPADES, Rank.FIVE), trump_state)

def test_can_play_any_heart_when_hearts_led():
    hand = [Card(Suit.HEARTS, Rank.FOUR), Card(Suit.HEARTS, Rank.KING)]
    trump_state = TrumpState(status=TrumpStatus.NONE)
    validate_follow_suit(hand, Suit.HEARTS, Card(Suit.HEARTS, Rank.FOUR), trump_state)

def test_can_cut_when_no_lead_suit():
    hand = [Card(Suit.SPADES, Rank.FOUR), Card(Suit.CLUBS, Rank.KING)]
    trump_state = TrumpState(status=TrumpStatus.NONE)
    validate_follow_suit(hand, Suit.HEARTS, Card(Suit.SPADES, Rank.FOUR), trump_state)

def test_must_play_trump_when_on_cut_and_trump_public():
    hand = [Card(Suit.SPADES, Rank.FOUR), Card(Suit.CLUBS, Rank.KING)]
    trump_state = TrumpState(status=TrumpStatus.PUBLIC, suit=Suit.SPADES)
    with pytest.raises(MustPlayTrump):
        validate_follow_suit(hand, Suit.HEARTS, Card(Suit.CLUBS, Rank.KING), trump_state)

def test_can_play_any_when_on_cut_no_trump_cards():
    hand = [Card(Suit.DIAMONDS, Rank.FOUR), Card(Suit.CLUBS, Rank.KING)]
    trump_state = TrumpState(status=TrumpStatus.PUBLIC, suit=Suit.SPADES)
    validate_follow_suit(hand, Suit.HEARTS, Card(Suit.CLUBS, Rank.KING), trump_state)

def test_no_trump_enforcement_when_hidden():
    hand = [Card(Suit.SPADES, Rank.FOUR), Card(Suit.CLUBS, Rank.KING)]
    trump_state = TrumpState(status=TrumpStatus.HIDDEN, suit=Suit.SPADES, hidden_rank=Rank.KING)
    validate_follow_suit(hand, Suit.HEARTS, Card(Suit.CLUBS, Rank.KING), trump_state)
