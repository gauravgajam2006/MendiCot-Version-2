import pytest
from mendicot.models import Card
from mendicot.enums import Suit, Rank, GamePhase

def test_team_with_more_tens_wins(game_state_4p, engine):
    game_state_4p.teams["TeamA"].tens_captured = 3
    game_state_4p.teams["TeamB"].tens_captured = 1
    game_state_4p.teams["TeamA"].tricks_won = 4
    game_state_4p.teams["TeamB"].tricks_won = 8
    
    winner = engine.determine_winner()
    assert winner == "TeamA"

def test_equal_tens_more_tricks_wins(game_state_4p, engine):
    game_state_4p.teams["TeamA"].tens_captured = 2
    game_state_4p.teams["TeamB"].tens_captured = 2
    game_state_4p.teams["TeamA"].tricks_won = 8
    game_state_4p.teams["TeamB"].tricks_won = 4
    
    winner = engine.determine_winner()
    assert winner == "TeamA"

def test_draw_when_tens_and_tricks_equal(game_state_4p, engine):
    game_state_4p.teams["TeamA"].tens_captured = 2
    game_state_4p.teams["TeamB"].tens_captured = 2
    game_state_4p.teams["TeamA"].tricks_won = 6
    game_state_4p.teams["TeamB"].tricks_won = 6
    
    winner = engine.determine_winner()
    assert winner is None

def test_ten_cards_only_score():
    assert Card(Suit.HEARTS, Rank.TEN).is_ten is True
    assert Card(Suit.HEARTS, Rank.NINE).is_ten is False

def test_trick_count_increments(game_state_4p, engine):
    game_state_4p.teams["TeamA"].tricks_won = 0
    scores = engine.calculate_score()
    assert scores["TeamA"]["tricks_won"] == 0

def test_full_game_4_players(four_players, engine):
    engine.create_game("full_game", four_players, hidden_trump_mode=False)
    state = engine.deal_cards()
    
    from mendicot.validators import validate_follow_suit
    from mendicot.exceptions import MustFollowSuit, MustPlayTrump

    while state.phase in (GamePhase.PLAYING, GamePhase.TRICK_RESOLUTION):
        if state.phase == GamePhase.TRICK_RESOLUTION:
            engine.resolve_trick()
        else:
            player_id = state.current_turn
            hand = state.hands[player_id]
            trick = state.current_trick
            
            valid_card = None
            if not trick.played_cards:
                valid_card = hand[0]
            else:
                for card in hand:
                    try:
                        validate_follow_suit(hand, trick.lead_suit, card, state.trump_state)
                        valid_card = card
                        break
                    except (MustFollowSuit, MustPlayTrump):
                        continue
                        
            engine.play_card(player_id, valid_card)
    
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)
    
    total_tricks = state.teams["TeamA"].tricks_won + state.teams["TeamB"].tricks_won
    assert total_tricks == 12
    
    total_tens = state.teams["TeamA"].tens_captured + state.teams["TeamB"].tens_captured
    assert total_tens == 4

def test_calculate_score_returns_current_team_scores(game_state_4p, engine):
    game_state_4p.teams["TeamA"].tens_captured = 2
    game_state_4p.teams["TeamA"].tricks_won = 5

    game_state_4p.teams["TeamB"].tens_captured = 1
    game_state_4p.teams["TeamB"].tricks_won = 7

    scores = engine.calculate_score()

    assert scores["TeamA"]["tens_captured"] == 2
    assert scores["TeamA"]["tricks_won"] == 5
    assert scores["TeamB"]["tens_captured"] == 1
    assert scores["TeamB"]["tricks_won"] == 7

def test_full_game_6_players(six_players, engine):
    engine.create_game(
        "full_game_6p",
        six_players,
        hidden_trump_mode=False
    )
    state = engine.deal_cards()

    from mendicot.validators import validate_follow_suit
    from mendicot.exceptions import MustFollowSuit, MustPlayTrump

    while state.phase in (
        GamePhase.PLAYING,
        GamePhase.TRICK_RESOLUTION
    ):
        if state.phase == GamePhase.TRICK_RESOLUTION:
            engine.resolve_trick()
        else:
            player_id = state.current_turn
            hand = state.hands[player_id]
            trick = state.current_trick

            valid_card = None

            if not trick.played_cards:
                valid_card = hand[0]
            else:
                for card in hand:
                    try:
                        validate_follow_suit(
                            hand,
                            trick.lead_suit,
                            card,
                            state.trump_state
                        )
                        valid_card = card
                        break
                    except (MustFollowSuit, MustPlayTrump):
                        continue

            engine.play_card(player_id, valid_card)

    assert state.phase in (
        GamePhase.GAME_OVER,
        GamePhase.DRAW
    )

    total_tricks = (
        state.teams["TeamA"].tricks_won
        + state.teams["TeamB"].tricks_won
    )

    assert total_tricks == 8

    total_tens = (
        state.teams["TeamA"].tens_captured
        + state.teams["TeamB"].tens_captured
    )

    assert total_tens == 4


def test_full_game_8_players(eight_players, engine):
    engine.create_game(
        "full_game_8p",
        eight_players,
        hidden_trump_mode=False
    )
    state = engine.deal_cards()

    from mendicot.validators import validate_follow_suit
    from mendicot.exceptions import MustFollowSuit, MustPlayTrump

    while state.phase in (
        GamePhase.PLAYING,
        GamePhase.TRICK_RESOLUTION
    ):
        if state.phase == GamePhase.TRICK_RESOLUTION:
            engine.resolve_trick()
        else:
            player_id = state.current_turn
            hand = state.hands[player_id]
            trick = state.current_trick

            valid_card = None

            if not trick.played_cards:
                valid_card = hand[0]
            else:
                for card in hand:
                    try:
                        validate_follow_suit(
                            hand,
                            trick.lead_suit,
                            card,
                            state.trump_state
                        )
                        valid_card = card
                        break
                    except (MustFollowSuit, MustPlayTrump):
                        continue

            engine.play_card(player_id, valid_card)

    assert state.phase in (
        GamePhase.GAME_OVER,
        GamePhase.DRAW
    )

    total_tricks = (
        state.teams["TeamA"].tricks_won
        + state.teams["TeamB"].tricks_won
    )

    assert total_tricks == 6

    total_tens = (
        state.teams["TeamA"].tens_captured
        + state.teams["TeamB"].tens_captured
    )

    assert total_tens == 4