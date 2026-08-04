import pytest
from mendicot.enums import GamePhase
from mendicot.exceptions import InvalidPhase


def test_host_can_select_trump_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True,
    )

    state = engine.select_trump_hider(
        four_players[0].player_id,
        four_players[1].player_id
    )

    assert state.selected_trump_hider_id == four_players[1].player_id
    assert state.trump_state.trump_hider_id == four_players[1].player_id


def test_non_host_cannot_select_trump_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True,
    )

    with pytest.raises(PermissionError):
        engine.select_trump_hider(
            four_players[1].player_id,
            four_players[2].player_id
        )


def test_host_can_select_first_player(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
    )

    state = engine.select_first_player(
        four_players[0].player_id,
        four_players[2].player_id
    )

    assert state.selected_first_player_id == four_players[2].player_id


def test_non_host_cannot_select_first_player(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
    )

    with pytest.raises(PermissionError):
        engine.select_first_player(
            four_players[1].player_id,
            four_players[2].player_id
        )


def test_invalid_player_id_select_trump_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True,
    )

    with pytest.raises(ValueError, match="Player not in game"):
        engine.select_trump_hider(
            four_players[0].player_id,
            "INVALID_PLAYER_ID"
        )


def test_invalid_player_id_select_trump_hider_in_phase(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True
    )
    engine.select_trump_hider("P1", "P1")
    engine.deal_cards()

    with pytest.raises(ValueError, match="Player not in game"):
        engine.select_trump_hider("INVALID_PLAYER_ID")


def test_invalid_player_id_select_first_player(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
    )

    with pytest.raises(ValueError, match="Player not in game"):
        engine.select_first_player(
            four_players[0].player_id,
            "INVALID_PLAYER_ID"
        )


def test_host_selection_of_trump_hider_updates_state(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True,
    )

    state = engine.select_trump_hider(
        four_players[0].player_id,
        "P3"
    )

    assert state.selected_trump_hider_id == "P3"
    assert state.trump_state.trump_hider_id == "P3"


def test_host_selection_of_first_player_updates_state(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
    )

    state = engine.select_first_player(
        four_players[0].player_id,
        "P3"
    )

    assert state.selected_first_player_id == "P3"


def test_normal_mode_selected_first_player_gets_turn(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=False
    )
    engine.select_first_player("P1", "P3")
    state = engine.deal_cards()

    assert state.phase == GamePhase.PLAYING
    assert state.current_turn == "P3"


def test_normal_mode_default_first_player_used_if_none_selected(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=False
    )
    state = engine.deal_cards()

    assert state.phase == GamePhase.PLAYING
    assert state.current_turn == four_players[0].player_id


def test_hidden_trump_mode_enters_hidden_trump_selection_after_dealing(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True
    )
    engine.select_trump_hider("P1", "P2")
    state = engine.deal_cards()

    assert state.phase == GamePhase.HIDDEN_TRUMP_SELECTION


def test_hidden_trump_deal_requires_a_precommitted_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True,
    )

    with pytest.raises(InvalidPhase, match="must be selected before dealing"):
        engine.deal_cards()


def test_hidden_trump_mode_selected_hider_can_select_card(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True
    )
    engine.select_trump_hider("P1", "P2")
    engine.deal_cards()

    state = engine.select_hidden_card("P2", 0)

    assert state.phase == GamePhase.HIDDEN_TRUMP_REVEAL
    assert state.trump_state.hidden_card_index == 0


def test_hidden_trump_mode_selected_first_player_respected_after_setup(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id,
        hidden_trump_mode=True
    )
    engine.select_trump_hider("P1", "P2")
    engine.select_first_player("P1", "P4")
    engine.deal_cards()

    engine.select_hidden_card("P2", 0)
    state = engine.complete_hidden_trump_setup()

    assert state.phase == GamePhase.PLAYING
    assert state.current_turn == "P4"