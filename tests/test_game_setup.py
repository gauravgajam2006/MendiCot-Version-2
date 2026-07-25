import pytest


def test_host_can_select_trump_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
    )

    state = engine.select_trump_hider(
        four_players[0].player_id,
        four_players[1].player_id
    )

    assert state.selected_trump_hider_id == four_players[1].player_id


def test_non_host_cannot_select_trump_hider(engine, four_players):
    engine.create_game(
        "game_setup",
        four_players,
        host_id=four_players[0].player_id
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