import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from mendicot.api.routes import app, connection_manager, room_manager, session_tokens
from mendicot.exceptions import RoomNotFound


client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    room_manager._rooms.clear()
    connection_manager.active_connections.clear()
    session_tokens.clear()
    yield


def create_room(player_count=4, trump_mode="normal"):
    return client.post(
        "/api/rooms", json={"player_count": player_count, "trump_mode": trump_mode}
    ).json()["room_id"]


def join(room_id, player_id, display_name=None):
    response = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": player_id, "display_name": display_name or player_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def receive_type(ws, message_type, predicate=None):
    for _ in range(20):
        message = ws.receive_json()
        if message["type"] == message_type and (predicate is None or predicate(message)):
            return message
    raise AssertionError(f"Did not receive {message_type}")


@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_explicit_lobby_leave_frees_configured_slot(player_count, trump_mode):
    room_id = create_room(player_count, trump_mode)
    players = [join(room_id, f"P{i}", f"Player {i}") for i in range(player_count)]

    with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as watcher, \
         client.websocket_connect(f"/ws/rooms/{room_id}?token={players[1]['session_token']}") as leaver:
        receive_type(watcher, "ROOM_STATE_UPDATE")
        receive_type(leaver, "ROOM_STATE_UPDATE")
        leaver.send_json({"action": "LEAVE_ROOM", "payload": {}})
        assert receive_type(leaver, "ACTION_SUCCESS")["payload"]["action"] == "LEAVE_ROOM"

        state = receive_type(
            watcher, "ROOM_STATE_UPDATE",
            lambda message: "P1" not in [p["player_id"] for p in message["payload"]["players"]],
        )["payload"]
        assert "P1" not in [player["player_id"] for player in state["players"]]
        assert all(player["player_id"] != "P1" for player in state["players"])

    assert "P1" not in room_manager.get_room(room_id).player_ids
    assert players[1]["session_token"] not in session_tokens
    replacement = join(room_id, "P-new", "Player 1")
    assert replacement["player_id"] == "P-new"
    assert room_manager.get_room(room_id).player_count == player_count


def test_host_leave_transfers_host_and_last_leave_deletes_room():
    room_id = create_room()
    host = join(room_id, "host", "Host")
    next_player = join(room_id, "next", "Next")

    with client.websocket_connect(f"/ws/rooms/{room_id}?token={next_player['session_token']}") as watcher, \
         client.websocket_connect(f"/ws/rooms/{room_id}?token={host['session_token']}") as host_ws:
        receive_type(watcher, "ROOM_STATE_UPDATE")
        receive_type(host_ws, "ROOM_STATE_UPDATE")
        host_ws.send_json({"action": "LEAVE_ROOM", "payload": {}})
        receive_type(host_ws, "ACTION_SUCCESS")
        state = receive_type(
            watcher, "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["host_id"] == "next",
        )["payload"]
        assert state["host_id"] == "next"
        assert [p["player_id"] for p in state["players"]] == ["next"]

        watcher.send_json({"action": "LEAVE_ROOM", "payload": {}})
        receive_type(watcher, "ACTION_SUCCESS")

    with pytest.raises(RoomNotFound):
        room_manager.get_room(room_id)
    assert room_id not in connection_manager.active_connections
    assert not session_tokens
    assert client.post(
        f"/api/rooms/{room_id}/join", json={"player_id": "again", "display_name": "Again"}
    ).status_code == 400
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={next_player['session_token']}"):
            pass


def test_ordinary_disconnect_keeps_player_and_capacity():
    room_id = create_room(4)
    players = [join(room_id, f"P{i}") for i in range(4)]

    with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[1]['session_token']}") as watcher:
        receive_type(watcher, "ROOM_STATE_UPDATE")
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as departing:
            receive_type(departing, "ROOM_STATE_UPDATE")

        state = receive_type(
            watcher, "ROOM_STATE_UPDATE",
            lambda message: next(
                p for p in message["payload"]["players"] if p["player_id"] == "P0"
            )["is_online"] is False,
        )["payload"]
        assert next(p for p in state["players"] if p["player_id"] == "P0")["is_online"] is False

    time.sleep(0.05)
    assert room_manager.get_room(room_id).player_ids == ["P0", "P1", "P2", "P3"]
    assert players[0]["session_token"] in session_tokens
    assert client.post(
        f"/api/rooms/{room_id}/join", json={"player_id": "P4", "display_name": "P4"}
    ).status_code == 400


def test_in_game_leave_returns_error_without_changing_engine_players():
    room_id = create_room(4)
    players = [join(room_id, f"P{i}") for i in range(4)]

    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players
        ]
        host_ws = websockets[0]
        host_ws.send_json({"action": "START_GAME", "payload": {}})
        receive_type(host_ws, "ACTION_SUCCESS")
        before = list(room_manager.get_room(room_id).engine.state.players)
        host_ws.send_json({"action": "LEAVE_ROOM", "payload": {}})
        error = receive_type(host_ws, "ERROR")
        assert "active game" in error["payload"]["message"]
        assert list(room_manager.get_room(room_id).engine.state.players) == before
        assert room_manager.get_room(room_id).player_ids == [player.player_id for player in before]
