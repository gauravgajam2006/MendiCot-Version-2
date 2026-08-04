import pytest
from contextlib import ExitStack
from fastapi.testclient import TestClient

from mendicot.api.routes import app, connection_manager, room_manager, session_tokens
from mendicot.enums import GamePhase


client = TestClient(app)


def receive_type(websocket, message_type, max_messages=20):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type:
            return message
    raise AssertionError(f"Did not receive {message_type}")


def create_room(player_count: int = 4, trump_mode: str = "normal"):
    return client.post(
        "/api/rooms",
        json={"player_count": player_count, "trump_mode": trump_mode},
    )


@pytest.fixture(autouse=True)
def reset_state():
    room_manager._rooms.clear()
    connection_manager.active_connections.clear()
    session_tokens.clear()
    yield


def test_create_and_join_room():
    response = create_room()
    assert response.status_code == 200
    room_id = response.json()["room_id"]

    response = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    )
    assert response.status_code == 200
    assert response.json()["room_id"] == room_id
    assert response.json()["player_id"] == "P1"
    assert "session_token" in response.json()


@pytest.mark.parametrize("room_id_transform", [
    str.lower,
    str.upper,
    lambda value: "".join(char.upper() if index % 2 else char.lower() for index, char in enumerate(value)),
])
def test_join_room_id_is_case_insensitive_and_returns_canonical_id(room_id_transform):
    room_id = create_room().json()["room_id"]

    response = client.post(
        f"/api/rooms/{room_id_transform(room_id)}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    )

    assert response.status_code == 200
    assert response.json()["room_id"] == room_id


def test_join_incorrect_room_id_fails():
    response = client.post(
        "/api/rooms/notaroom/join",
        json={"player_id": "P1", "display_name": "Alice"},
    )

    assert response.status_code == 400


def test_websocket_accepts_uppercase_room_id_for_matching_token():
    room_id = create_room().json()["room_id"]
    token = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    ).json()["session_token"]

    with client.websocket_connect(f"/ws/rooms/{room_id.upper()}?token={token}") as ws:
        assert ws.receive_json()["type"] == "ROOM_STATE_UPDATE"


def test_websocket_rejects_token_for_another_room():
    room_id = create_room().json()["room_id"]
    other_room_id = create_room().json()["room_id"]
    token = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    ).json()["session_token"]

    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/rooms/{other_room_id.upper()}?token={token}"):
            pass

def test_websocket_connect_and_broadcast():
    room_id = create_room().json()["room_id"]
    token1 = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    ).json()["session_token"]

    with client.websocket_connect(f"/ws/rooms/{room_id}?token={token1}") as ws1:
        data = ws1.receive_json()
        assert data["type"] == "ROOM_STATE_UPDATE"
        assert data["payload"]["players"][0]["is_online"] is True

        token2 = client.post(
            f"/api/rooms/{room_id}/join",
            json={"player_id": "P2", "display_name": "Bob"},
        ).json()["session_token"]
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={token2}") as ws2:
            assert ws2.receive_json()["type"] == "ROOM_STATE_UPDATE"
            assert len(ws1.receive_json()["payload"]["players"]) == 2


def test_invalid_token_rejected():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/rooms/ANY?token=invalid_token"):
            pass


def test_start_game_flow():
    room_id = create_room().json()["room_id"]
    tokens = [
        client.post(
            f"/api/rooms/{room_id}/join",
            json={"player_id": f"P{i}", "display_name": f"P{i}"},
        ).json()["session_token"]
        for i in range(1, 5)
    ]
    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(f"/ws/rooms/{room_id}?token={token}")
            )
            for token in tokens
        ]
        host = websockets[0]
        host.send_json(
            {"action": "START_GAME", "payload": {"hidden_trump_mode": False}}
        )
        assert receive_type(host, "ACTION_SUCCESS")["payload"]["action"] == "START_GAME"
        assert receive_type(host, "ROOM_STATE_UPDATE")["payload"]["status"] == "GAME_SETUP"
        assert (
            receive_type(host, "GAME_STATE_UPDATE")["payload"]["phase"]
            == GamePhase.FIRST_PLAYER_SELECTION.value
        )


def test_error_handling_invalid_action():
    room_id = create_room().json()["room_id"]
    token = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    ).json()["session_token"]
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={token}") as ws:
        ws.receive_json()
        ws.send_json({"action": "MADE_UP_ACTION"})
        assert ws.receive_json()["payload"]["code"] == "UNKNOWN_ACTION"
        ws.send_json({"action": "PLAY_CARD", "payload": {"suit": "HEARTS", "rank": 14}})
        assert ws.receive_json()["payload"]["code"] == "MendiCotError"


def test_create_room_configuration_contract():
    normal = create_room(4, "normal")
    hidden = create_room(6, "hidden")
    eight = create_room(8, "normal")
    assert normal.status_code == hidden.status_code == eight.status_code == 200
    assert room_manager.get_room(normal.json()["room_id"]).configured_player_count == 4
    hidden_room = room_manager.get_room(hidden.json()["room_id"])
    assert hidden_room.configured_player_count == 6
    assert hidden_room.trump_mode == "hidden"
    assert room_manager.get_room(eight.json()["room_id"]).configured_player_count == 8


def test_openapi_documents_create_room_request():
    schema = client.get("/openapi.json").json()["components"]["schemas"]["CreateRoomRequest"]
    assert schema["properties"]["player_count"]["enum"] == [4, 6, 8]
    assert schema["properties"]["trump_mode"]["enum"] == ["normal", "hidden"]


@pytest.mark.parametrize("player_count", [3, 5, 7, 9])
def test_create_room_rejects_invalid_player_count(player_count):
    assert create_room(player_count, "normal").status_code == 422


@pytest.mark.parametrize("trump_mode", ["", "public", "HIDDEN"])
def test_create_room_rejects_invalid_trump_mode(trump_mode):
    assert create_room(4, trump_mode).status_code == 422


def test_room_state_broadcast_includes_configuration():
    room_id = create_room(6, "hidden").json()["room_id"]
    token = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P1", "display_name": "Alice"},
    ).json()["session_token"]
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={token}") as ws:
        state = ws.receive_json()["payload"]
        assert state["player_count"] == 6
        assert state["trump_mode"] == "hidden"


def test_start_game_uses_stored_trump_mode_and_ignores_client_override():
    room_id = create_room(4, "hidden").json()["room_id"]
    tokens = [
        client.post(
            f"/api/rooms/{room_id}/join",
            json={"player_id": f"P{i}", "display_name": f"P{i}"},
        ).json()["session_token"]
        for i in range(1, 5)
    ]
    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(f"/ws/rooms/{room_id}?token={token}")
            )
            for token in tokens
        ]
        host = websockets[0]
        host.send_json(
            {"action": "START_GAME", "payload": {"hidden_trump_mode": False}}
        )
        assert receive_type(host, "ACTION_SUCCESS")["payload"]["action"] == "START_GAME"
        assert room_manager.get_room(room_id).engine.state.hidden_trump_mode is True
