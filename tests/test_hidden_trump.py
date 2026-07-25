import pytest
from mendicot.enums import GamePhase, TrumpStatus, Suit
from mendicot.exceptions import TrumpAlreadyRevealed, InvalidTrumpAction, MustPlayTrump
from mendicot.models import Trick, PlayedCard, Card

def test_hidden_trump_game_starts_in_selection_phase(game_state_hidden_4p):
    assert game_state_hidden_4p.phase == GamePhase.HIDDEN_TRUMP_SELECTION

def test_select_trump_hider(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    assert game_state_hidden_4p.trump_state.trump_hider_id == "P1"

def test_blind_selection_by_position(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    assert game_state_hidden_4p.phase == GamePhase.HIDDEN_TRUMP_REVEAL

def test_card_temporarily_revealed(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    assert game_state_hidden_4p.trump_state.hidden_card_index == 0
    assert game_state_hidden_4p.trump_state.suit is not None
    assert game_state_hidden_4p.trump_state.hidden_rank is not None

def test_suit_becomes_trump(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    expected_suit = game_state_hidden_4p.hands["P1"][0].suit
    engine.complete_hidden_trump_setup()
    assert game_state_hidden_4p.trump_state.suit == expected_suit

def test_rank_hidden_after_setup(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    assert game_state_hidden_4p.trump_state.status == TrumpStatus.HIDDEN
    assert game_state_hidden_4p.trump_state.hidden_rank is not None
    
    view = engine.get_player_view("P2")
    assert view["trump_state"]["hidden_rank"] is None

def test_card_returns_to_hider_hand(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    assert len(game_state_hidden_4p.hands["P1"]) == 12

def test_first_lead_is_player_after_hider(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    assert game_state_hidden_4p.current_turn == "P2"

def test_reveal_is_optional(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.play_card("P3", game_state_hidden_4p.hands["P3"][0])
    assert game_state_hidden_4p.trump_state.status == TrumpStatus.HIDDEN

def test_reveal_is_separate_action(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    assert game_state_hidden_4p.trump_state.status == TrumpStatus.PUBLIC
    assert game_state_hidden_4p.current_turn == "P3"

def test_after_reveal_trump_becomes_public(game_state_hidden_4p, engine):
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    assert game_state_hidden_4p.trump_state.status == TrumpStatus.PUBLIC

def test_must_play_trump_after_reveal_on_cut(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 14)] + [Card(Suit.CLUBS, 14)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    
    with pytest.raises(MustPlayTrump):
        engine.play_card("P3", Card(Suit.SPADES, 13))

def test_multiple_trump_cards_allow_choice(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.CLUBS, 13), Card(Suit.CLUBS, 14)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    engine.play_card("P3", Card(Suit.CLUBS, 13))

def test_no_trump_cards_play_anything(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    engine.play_card("P3", game_state_hidden_4p.hands["P3"][0])

def test_hidden_trump_still_wins_trick(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    trick = Trick(
        lead_player_id="P2",
        lead_suit=Suit.HEARTS,
        played_cards=[
            PlayedCard("P2", Card(Suit.HEARTS, 14)),
            PlayedCard("P3", Card(Suit.CLUBS, 3)),
            PlayedCard("P4", Card(Suit.HEARTS, 3)),
            PlayedCard("P1", Card(Suit.HEARTS, 4))
        ]
    )
    winner = engine._determine_trick_winner(trick, game_state_hidden_4p.trump_state)
    assert winner == "P3"

def test_trump_already_revealed_error(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.SPADES, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    engine.reveal_trump("P3")
    
    with pytest.raises(TrumpAlreadyRevealed):
        engine.reveal_trump("P3")

def test_cannot_reveal_when_not_on_cut(game_state_hidden_4p, engine):
    game_state_hidden_4p.hands["P1"] = [Card(Suit.CLUBS, rank) for rank in range(3, 15)]
    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()
    
    game_state_hidden_4p.hands["P2"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    game_state_hidden_4p.hands["P3"] = [Card(Suit.HEARTS, rank) for rank in range(3, 15)]
    
    engine.play_card("P2", game_state_hidden_4p.hands["P2"][0])
    
    with pytest.raises(InvalidTrumpAction):
        engine.reveal_trump("P3")

def test_hidden_trump_auto_reveals_when_player_is_on_cut_and_has_trump(
    game_state_hidden_4p,
    engine
):
    game_state_hidden_4p.hands["P1"] = [
        Card(Suit.CLUBS, rank) for rank in range(3, 15)
    ]

    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()

    game_state_hidden_4p.hands["P2"] = [
        Card(Suit.HEARTS, rank) for rank in range(3, 15)
    ]

    game_state_hidden_4p.hands["P3"] = [
        Card(Suit.CLUBS, 13),
        Card(Suit.CLUBS, 14)
    ]

    engine.play_card(
        "P2",
        game_state_hidden_4p.hands["P2"][0]
    )

    engine.play_card(
        "P3",
        Card(Suit.CLUBS, 13)
    )

    assert (
        game_state_hidden_4p.trump_state.status
        == TrumpStatus.PUBLIC
    )

def test_hidden_trump_does_not_reveal_when_player_has_no_trump(
    game_state_hidden_4p,
    engine
):
    game_state_hidden_4p.hands["P1"] = [
        Card(Suit.CLUBS, rank) for rank in range(3, 15)
    ]

    engine.select_trump_hider("P1")
    engine.select_hidden_card("P1", 0)
    engine.complete_hidden_trump_setup()

    game_state_hidden_4p.hands["P2"] = [
        Card(Suit.HEARTS, rank) for rank in range(3, 15)
    ]

    game_state_hidden_4p.hands["P3"] = [
        Card(Suit.SPADES, rank) for rank in range(3, 15)
    ]

    engine.play_card(
        "P2",
        game_state_hidden_4p.hands["P2"][0]
    )

    engine.play_card(
        "P3",
        game_state_hidden_4p.hands["P3"][0]
    )

    assert (
        game_state_hidden_4p.trump_state.status
        == TrumpStatus.HIDDEN
    )