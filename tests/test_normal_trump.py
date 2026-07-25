import pytest
from mendicot.enums import TrumpStatus, Suit, GamePhase
from mendicot.models import Player, Card
from mendicot.exceptions import (
    InvalidPlayerCount,
    NotPlayersTurn,
    CardNotOwned,
    InvalidPhase,
    MustPlayTrump
)

def test_trump_starts_as_none(game_state_4p):
    assert game_state_4p.trump_state.status == TrumpStatus.NONE
    assert game_state_4p.trump_state.suit is None

def test_first_cut_sets_trump(game_state_4p, engine):
    game_state_4p.hands["P1"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_4p.hands["P2"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P1", game_state_4p.hands["P1"][0])
    
    cut_card = game_state_4p.hands["P2"][0]
    engine.play_card("P2", cut_card)
    
    assert game_state_4p.trump_state.suit == Suit.SPADES
    assert game_state_4p.trump_state.status == TrumpStatus.PUBLIC

def test_trump_suit_is_correct(game_state_4p, engine):
    game_state_4p.hands["P1"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_4p.hands["P2"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    engine.play_card("P1", game_state_4p.hands["P1"][0])
    engine.play_card("P2", game_state_4p.hands["P2"][0])
    assert game_state_4p.trump_state.suit == Suit.SPADES

def test_trump_is_public_after_cut(game_state_4p, engine):
    game_state_4p.hands["P1"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_4p.hands["P2"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    engine.play_card("P1", game_state_4p.hands["P1"][0])
    engine.play_card("P2", game_state_4p.hands["P2"][0])
    assert game_state_4p.trump_state.status == TrumpStatus.PUBLIC

def test_must_play_trump_on_cut_after_reveal(game_state_4p, engine):
    game_state_4p.hands["P1"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_4p.hands["P2"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    game_state_4p.hands["P3"] = [Card(Suit.CLUBS, rank) for rank in range(3, 14)] + [Card(Suit.SPADES, 14)]
    
    engine.play_card("P1", game_state_4p.hands["P1"][0])
    engine.play_card("P2", game_state_4p.hands["P2"][0])
    
    with pytest.raises(MustPlayTrump):
        engine.play_card("P3", Card(Suit.CLUBS, 13))

def test_player_without_trump_plays_anything(game_state_4p, engine):
    game_state_4p.hands["P1"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_4p.hands["P2"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    game_state_4p.hands["P3"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    
    engine.play_card("P1", game_state_4p.hands["P1"][0])
    engine.play_card("P2", game_state_4p.hands["P2"][0])
    engine.play_card("P3", game_state_4p.hands["P3"][0])

def test_cannot_create_game_with_3_players(engine):
    players = [
        Player("P1", "TeamA", 0),
        Player("P2", "TeamB", 1),
        Player("P3", "TeamA", 2)
    ]
    with pytest.raises(InvalidPlayerCount):
        engine.create_game(
    "game1",
    players,
    host_id=players[0].player_id
)

def test_wrong_player_turn_rejected(game_state_4p, engine):
    with pytest.raises(NotPlayersTurn):
        engine.play_card("P2", game_state_4p.hands["P2"][0])

def test_card_not_in_hand_rejected(game_state_4p, engine):
    card_not_owned = game_state_4p.hands["P2"][0]
    with pytest.raises(CardNotOwned):
        engine.play_card("P1", card_not_owned)

def test_game_phase_validation(engine, four_players):
    engine.create_game(
    "game1",
    four_players,
    host_id=four_players[0].player_id
)
    with pytest.raises(InvalidPhase):
        engine.play_card("P1", Card(Suit.HEARTS, 3))
