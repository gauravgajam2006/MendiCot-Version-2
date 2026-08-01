import asyncio
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from mendicot.api import routes


client = TestClient(routes.app)


class Socket:
    async def accept(self):
        pass

    async def close(self, **kwargs):
        pass

    async def send_json(self, message):
        pass


@pytest.fixture(autouse=True)
def reset_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()
    yield
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


def create_room():
    response = client.post(
        "/api/rooms",
        json={"player_count": 4, "trump_mode": "normal"},
    )
    assert response.status_code == 200
    return response.json()["room_id"]


def join(room_id, player_id, display_name):
    response = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": player_id, "display_name": display_name},
    )
    assert response.status_code == 200
    return response.json()


def validate(room_id, player_id, session_token):
    return client.post(
        f"/api/rooms/{room_id}/sessions/validate",
        json={"player_id": player_id, "session_token": session_token},
    )


def assert_error(response, status_code, code, message):
    assert response.status_code == status_code
    assert response.json() == {"detail": {"code": code, "message": message}}


def test_valid_offline_player_within_grace_period():
    room_id = create_room()
    player = join(room_id, "P1", "Alice")

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()

    response = validate(room_id, "P1", player["session_token"])

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "room_id": room_id,
        "player_id": "P1",
        "display_name": "Alice",
        "room_status": "WAITING",
        "player_online": False,
    }


def test_valid_online_player():
    room_id = create_room()
    player = join(room_id, "P1", "Alice")

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={player['session_token']}"
    ) as websocket:
        websocket.receive_json()
        response = validate(room_id, "P1", player["session_token"])
        assert response.status_code == 200
        assert response.json()["player_online"] is True


def test_player_removed_after_timeout_returns_session_expired(monkeypatch):
    room_id = create_room()
    departing = join(room_id, "departing", "Departing")
    join(room_id, "remaining", "Remaining")
    monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)

    async def expire_player():
        socket = Socket()
        await routes.connection_manager.connect(room_id, "departing", socket)
        await routes._handle_socket_disconnect(room_id, "departing", socket)
        await asyncio.sleep(0.03)

    asyncio.run(expire_player())

    response = validate(room_id, "departing", departing["session_token"])
    assert_error(
        response, 410, "SESSION_EXPIRED", "Player session has expired."
    )


def test_intentionally_left_player_returns_session_expired():
    room_id = create_room()
    departing = join(room_id, "departing", "Departing")
    join(room_id, "remaining", "Remaining")

    asyncio.run(routes._remove_lobby_player(room_id, "departing"))

    response = validate(room_id, "departing", departing["session_token"])
    assert_error(
        response, 410, "SESSION_EXPIRED", "Player session has expired."
    )


def test_deleted_room_returns_room_not_found():
    room_id = create_room()
    player = join(room_id, "P1", "Alice")
    routes.room_manager.delete_room(room_id)

    response = validate(room_id, "P1", player["session_token"])
    assert_error(response, 404, "ROOM_NOT_FOUND", "Room no longer exists.")


def test_wrong_token_returns_invalid_session():
    room_id = create_room()
    join(room_id, "P1", "Alice")

    response = validate(room_id, "P1", "not-a-session-token")
    assert_error(response, 401, "INVALID_SESSION", "Session token is invalid.")


def test_token_for_another_player_returns_invalid_session_without_disclosure():
    room_id = create_room()
    alice = join(room_id, "P1", "Alice")
    join(room_id, "P2", "Bob")

    response = validate(room_id, "P2", alice["session_token"])
    assert_error(response, 401, "INVALID_SESSION", "Session token is invalid.")
    assert "Alice" not in response.text
    assert "P1" not in response.text


@pytest.mark.parametrize(
    "transform",
    [
        str.lower,
        str.upper,
        lambda value: "".join(
            character.upper() if index % 2 else character.lower()
            for index, character in enumerate(value)
        ),
    ],
)
def test_validation_normalizes_room_id_and_returns_canonical_id(transform):
    room_id = create_room()
    player = join(room_id, "P1", "Alice")

    response = validate(transform(room_id), "P1", player["session_token"])

    assert response.status_code == 200
    assert response.json()["room_id"] == room_id


def test_validation_is_read_only_does_not_consume_capacity_or_mark_online():
    room_id = create_room()
    player = join(room_id, "P1", "Alice")
    room = routes.room_manager.get_room(room_id)
    room_state_before = deepcopy(room.get_state())
    sessions_before = deepcopy(routes.session_tokens)
    invalidated_before = deepcopy(routes.invalidated_session_tokens)
    connections_before = deepcopy(routes.connection_manager.active_connections)

    response = validate(room_id, "P1", player["session_token"])

    assert response.status_code == 200
    assert response.json()["player_online"] is False
    assert room.get_state() == room_state_before
    assert room.player_count == 1
    assert room.configured_player_count == 4
    assert routes.session_tokens == sessions_before
    assert routes.invalidated_session_tokens == invalidated_before
    assert routes.connection_manager.active_connections == connections_before

    join(room_id, "P2", "Bob")
    join(room_id, "P3", "Cara")
    join(room_id, "P4", "Dev")
    assert room.player_count == room.configured_player_count
