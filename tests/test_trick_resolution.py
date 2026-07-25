import pytest
from mendicot.models import Trick, PlayedCard, Card, TrumpState
from mendicot.enums import Suit, Rank, TrumpStatus, GamePhase

def test_highest_lead_suit_wins_no_trump(engine):
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.HEARTS, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )
    trump_state = TrumpState(status=TrumpStatus.NONE)
    winner = engine._determine_trick_winner(trick, trump_state)
    assert winner == "P4"

def test_single_trump_beats_lead_suit(engine):
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.SPADES, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )
    trump_state = TrumpState(status=TrumpStatus.PUBLIC, suit=Suit.SPADES)
    winner = engine._determine_trick_winner(trick, trump_state)
    assert winner == "P3"

def test_highest_trump_wins_multiple_trumps(engine):
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.SPADES, Rank.NINE)),
            PlayedCard("P4", Card(Suit.SPADES, Rank.THREE))
        ]
    )
    trump_state = TrumpState(status=TrumpStatus.PUBLIC, suit=Suit.SPADES)
    winner = engine._determine_trick_winner(trick, trump_state)
    assert winner == "P3"

def test_non_lead_non_trump_cannot_win(engine):
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.DIAMONDS, Rank.KING)),
            PlayedCard("P3", Card(Suit.CLUBS, Rank.ACE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.THREE))
        ]
    )
    trump_state = TrumpState(status=TrumpStatus.NONE)
    winner = engine._determine_trick_winner(trick, trump_state)
    assert winner == "P1"

def test_trick_cards_captured_by_winner_team(game_state_4p, engine):
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.HEARTS, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )
    game_state_4p.current_trick = trick
    engine.resolve_trick()
    
    assert len(game_state_4p.teams["TeamB"].captured_cards) == 4
    assert game_state_4p.teams["TeamB"].tricks_won == 1

def test_tens_counted_correctly(game_state_4p, engine):
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION
    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.TEN)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.HEARTS, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )
    game_state_4p.current_trick = trick
    engine.resolve_trick()
    
    assert game_state_4p.teams["TeamB"].tens_captured == 1

def test_trick_winner_leads_next_trick(game_state_4p, engine):
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION

    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.HEARTS, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )

    game_state_4p.current_trick = trick

    engine.resolve_trick()

    assert game_state_4p.current_turn == "P4"
    assert game_state_4p.phase == GamePhase.PLAYING

def test_game_ends_when_all_cards_are_played(game_state_4p, engine):
    game_state_4p.phase = GamePhase.TRICK_RESOLUTION

    game_state_4p.hands = {
        "P1": [],
        "P2": [],
        "P3": [],
        "P4": []
    }

    trick = Trick(
        lead_player_id="P1",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P1", Card(Suit.HEARTS, Rank.EIGHT)),
            PlayedCard("P2", Card(Suit.HEARTS, Rank.KING)),
            PlayedCard("P3", Card(Suit.HEARTS, Rank.THREE)),
            PlayedCard("P4", Card(Suit.HEARTS, Rank.ACE))
        ]
    )

    game_state_4p.current_trick = trick

    engine.resolve_trick()

    assert game_state_4p.phase in (
        GamePhase.GAME_OVER,
        GamePhase.DRAW
    )

    assert game_state_4p.current_turn is None    