from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from mendicot.api import routes
from mendicot.enums import GamePhase, TeamId
from mendicot.exceptions import (
    InvalidPhase,
    InvalidTeamName,
    PlayerNotInRoom,
    TeamNameTooLong,
)
from mendicot.room import GameRoom


client = TestClient(routes.app)
EXPECTED_NAMES = {"TeamA": "Notorious Squad", "TeamB": "Royal Aces"}


def _clear_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    for task in list(routes._trick_resolution_tasks.values()):
        task.cancel()
    routes._trick_resolution_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


@pytest.fixture(autouse=True)
def reset_state():
    _clear_state()
    yield
    _clear_state()


def create_and_join(player_count=4, trump_mode="normal", joined_count=None):
    response = client.post(
        "/api/rooms",
        json={"player_count": player_count, "trump_mode": trump_mode},
    )
    assert response.status_code == 200, response.text
    room_id = response.json()["room_id"]
    joined_count = player_count if joined_count is None else joined_count
    players = []
    for index in range(joined_count):
        response = client.post(
            f"/api/rooms/{room_id}/join",
            json={"player_id": f"P{index}", "display_name": f"Player {index}"},
        )
        assert response.status_code == 200, response.text
        players.append(response.json())
    return room_id, players


def receive_type(websocket, message_type, predicate=None, max_messages=100):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type and (
            predicate is None or predicate(message)
        ):
            return message
    raise AssertionError(f"Did not receive {message_type}")


def connect_all(stack, room_id, players):
    sockets = []
    for player in players:
        websocket = stack.enter_context(
            client.websocket_connect(
                f"/ws/rooms/{room_id}?token={player['session_token']}"
            )
        )
        receive_type(websocket, "ROOM_STATE_UPDATE")
        sockets.append(websocket)
    return sockets


def populate_room(player_count=4, trump_mode="normal"):
    room = GameRoom(
        f"room-{player_count}-{trump_mode}",
        configured_player_count=player_count,
        trump_mode=trump_mode,
    )
    for index in range(player_count):
        room.add_player(f"P{index}", f"Player {index}")
        room.set_player_online(f"P{index}", True)
    return room


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_room_creation_has_default_names_for_every_supported_size(player_count):
    room = GameRoom("defaults", configured_player_count=player_count)

    assert room.get_state()["team_names"] == {
        "TeamA": "Team Maroon",
        "TeamB": "Team Gold",
    }


def test_players_rename_only_their_current_team_and_switching_does_not_move_names():
    room = GameRoom("permissions", configured_player_count=4)
    room.add_player("A")
    room.add_player("B")

    assert room.rename_team("A", "Notorious Squad") == "Notorious Squad"
    assert room.rename_team("B", "Royal Aces") == "Royal Aces"
    assert room.get_state()["team_names"] == EXPECTED_NAMES

    room.switch_team("A", "TeamB")
    room.rename_team("A", "Gold Standard")

    assert room.get_state()["team_names"] == {
        "TeamA": "Notorious Squad",
        "TeamB": "Gold Standard",
    }
    with pytest.raises(PlayerNotInRoom):
        room.rename_team("missing", "No Team")


def test_team_name_normalization_validation_unicode_and_idempotency():
    room = GameRoom("validation", configured_player_count=4)
    room.add_player("A")

    assert room.rename_team("A", "  Café Kings — २  ") == "Café Kings — २"
    assert room.rename_team("A", "Café Kings — २") == "Café Kings — २"
    assert room.team_names[TeamId.TEAM_A] == "Café Kings — २"

    for invalid_name in ("", "   ", None, "Bad\nName", "Bad\tName", "Bad\u200bName"):
        with pytest.raises(InvalidTeamName):
            room.rename_team("A", invalid_name)

    room.rename_team("A", "x" * 24)
    with pytest.raises(TeamNameTooLong):
        room.rename_team("A", "x" * 25)


@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_names_survive_setup_gameplay_results_and_all_room_sizes(
    player_count, trump_mode
):
    room = populate_room(player_count, trump_mode)
    room.rename_team("P0", EXPECTED_NAMES["TeamA"])
    room.rename_team("P1", EXPECTED_NAMES["TeamB"])

    room.start_game("P0")
    if trump_mode == "hidden":
        room.select_trump_hider("P0", "P0")
    assert room.get_state()["team_names"] == EXPECTED_NAMES
    with pytest.raises(InvalidPhase):
        room.rename_team("P0", "Too Late")

    setup_view = routes._enrich_game_view(
        room, room.engine.get_player_view("P0")
    )
    assert setup_view["team_names"] == EXPECTED_NAMES

    room.select_first_player("P0", "P0")
    for phase in (
        GamePhase.HIDDEN_TRUMP_SELECTION,
        GamePhase.HIDDEN_TRUMP_REVEAL,
        GamePhase.PLAYING,
        GamePhase.TRICK_RESOLUTION,
        GamePhase.GAME_OVER,
        GamePhase.DRAW,
    ):
        room.engine.state.phase = phase
        if phase in (GamePhase.GAME_OVER, GamePhase.DRAW):
            room.engine._deal_audit.audit_status = "VERIFIED"
        view = routes._enrich_game_view(
            room, room.engine.get_player_view("P0")
        )
        assert view["team_names"] == EXPECTED_NAMES
        assert set(view["teams"]) == {"TeamA", "TeamB"}


def test_cancelled_setup_preserves_names_when_returning_to_lobby():
    room = populate_room()
    room.rename_team("P0", EXPECTED_NAMES["TeamA"])
    room.rename_team("P1", EXPECTED_NAMES["TeamB"])

    room.start_game("P0")
    room.cancel_game_setup("P0")

    assert room.get_state()["team_names"] == EXPECTED_NAMES
    room.rename_team("P0", "Renamed Again")


def test_websocket_rename_contract_broadcast_permission_errors_and_reconnect():
    room_id, players = create_and_join(joined_count=2)

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        team_a_socket, team_b_socket = sockets

        team_a_socket.send_json(
            {
                "action": "RENAME_TEAM",
                "payload": {
                    "name": "  Notorious Squad  ",
                    "team_id": "TeamB",
                },
            }
        )
        success = receive_type(team_a_socket, "ACTION_SUCCESS")
        assert success["payload"] == {"action": "RENAME_TEAM"}
        team_a_state = receive_type(
            team_a_socket,
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["team_names"]["TeamA"]
            == "Notorious Squad",
        )["payload"]
        team_b_state = receive_type(
            team_b_socket,
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["team_names"]["TeamA"]
            == "Notorious Squad",
        )["payload"]
        assert team_a_state["team_names"] == team_b_state["team_names"] == {
            "TeamA": "Notorious Squad",
            "TeamB": "Team Gold",
        }

        team_b_socket.send_json(
            {"action": "RENAME_TEAM", "payload": {"name": "Royal Aces"}}
        )
        assert receive_type(team_b_socket, "ACTION_SUCCESS")["payload"] == {
            "action": "RENAME_TEAM"
        }
        receive_type(
            team_b_socket,
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["team_names"] == EXPECTED_NAMES,
        )

        for name, code in (
            ("", "INVALID_TEAM_NAME"),
            ("x" * 25, "TEAM_NAME_TOO_LONG"),
        ):
            team_a_socket.send_json(
                {"action": "RENAME_TEAM", "payload": {"name": name}}
            )
            error = receive_type(team_a_socket, "ERROR")
            assert error["payload"]["action"] == "RENAME_TEAM"
            assert error["payload"]["code"] == code

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={players[0]['session_token']}"
    ) as replacement:
        resumed = receive_type(replacement, "ROOM_STATE_UPDATE")["payload"]
        assert resumed["team_names"] == EXPECTED_NAMES


@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_websocket_game_states_preserve_names_and_post_start_rename_is_rejected(
    trump_mode,
):
    room_id, players = create_and_join(trump_mode=trump_mode)

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        host = sockets[0]

        host.send_json(
            {"action": "RENAME_TEAM", "payload": {"name": EXPECTED_NAMES["TeamA"]}}
        )
        receive_type(host, "ACTION_SUCCESS")
        receive_type(
            host,
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["team_names"]["TeamA"]
            == EXPECTED_NAMES["TeamA"],
        )

        sockets[1].send_json(
            {"action": "RENAME_TEAM", "payload": {"name": EXPECTED_NAMES["TeamB"]}}
        )
        receive_type(sockets[1], "ACTION_SUCCESS")
        receive_type(
            sockets[1],
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["team_names"] == EXPECTED_NAMES,
        )

        host.send_json({"action": "START_GAME"})
        receive_type(host, "ACTION_SUCCESS")
        setup_state = receive_type(
            host,
            "GAME_STATE_UPDATE",
            lambda message: message["payload"]["room_status"] == "GAME_SETUP",
        )
        assert setup_state["payload"]["team_names"] == EXPECTED_NAMES

        host.send_json(
            {"action": "RENAME_TEAM", "payload": {"name": "Too Late"}}
        )
        error = receive_type(host, "ERROR")
        assert error["payload"]["code"] == "INVALID_PHASE"
        assert routes.room_manager.get_room(room_id).get_state()["team_names"] == EXPECTED_NAMES

        if trump_mode == "hidden":
            host.send_json(
                {"action": "SELECT_TRUMP_HIDER", "payload": {"player_id": "P0"}}
            )
            receive_type(host, "ACTION_SUCCESS")

        host.send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
        )
        receive_type(host, "ACTION_SUCCESS")
        game_state = receive_type(
            host,
            "GAME_STATE_UPDATE",
            lambda message: message["payload"]["room_status"] == "IN_GAME",
        )
        assert game_state["payload"]["team_names"] == EXPECTED_NAMES