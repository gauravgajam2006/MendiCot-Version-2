import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from mendicot.api import routes
from mendicot.enums import GamePhase
from mendicot.exceptions import InvalidPhase


client = TestClient(routes.app)


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


def create_joined_room(player_count=4, trump_mode="normal"):
    room_id = client.post(
        "/api/rooms", json={"player_count": player_count, "trump_mode": trump_mode}
    ).json()["room_id"]
    players = [
        client.post(
            f"/api/rooms/{room_id}/join",
            json={"player_id": f"P{index}", "display_name": f"Player {index}"},
        ).json()
        for index in range(player_count)
    ]
    return room_id, players


def connect_all(stack, room_id, players):
    return [
        stack.enter_context(
            client.websocket_connect(
                f"/ws/rooms/{room_id}?token={player['session_token']}"
            )
        )
        for player in players
    ]


def receive_type(websocket, message_type, predicate=None, max_messages=50):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type and (
            predicate is None or predicate(message)
        ):
            return message
    raise AssertionError(f"Did not receive {message_type}")


def start_setup(host):
    host.send_json({"action": "START_GAME", "payload": {}})
    return receive_type(
        host,
        "ACTION_SUCCESS",
        lambda message: message["payload"]["action"] == "START_GAME",
    )


def setup_state(message, host_id="P0"):
    payload = message["payload"]
    return (
        payload["phase"] == GamePhase.FIRST_PLAYER_SELECTION.value
        and payload["host_id"] == host_id
        and payload["current_player_id"] is None
    )


def test_start_broadcasts_one_authoritative_setup_state_to_every_player():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])

        states = [
            receive_type(socket, "GAME_STATE_UPDATE", setup_state)["payload"]
            for socket in sockets
        ]

    assert all(state["room_id"] == room_id for state in states)
    assert all(state["room_status"] == "GAME_SETUP" for state in states)
    assert all(state["players"] == states[0]["players"] for state in states)
    room = routes.room_manager.get_room(room_id)
    assert room.status.value == "GAME_SETUP"
    assert room.engine.state.phase == GamePhase.FIRST_PLAYER_SELECTION


def test_host_can_cancel_setup_reset_engine_and_start_again():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        first_engine = routes.room_manager.get_room(room_id).engine

        sockets[0].send_json({"action": "CANCEL_GAME_SETUP", "payload": {}})
        assert receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "CANCEL_GAME_SETUP",
        )
        for socket in sockets:
            state = receive_type(
                socket,
                "ROOM_STATE_UPDATE",
                lambda message: message["payload"]["status"] == "WAITING",
            )["payload"]
            assert state["host_id"] == "P0"

        assert routes.room_manager.get_room(room_id).engine is None
        start_setup(sockets[0])

    room = routes.room_manager.get_room(room_id)
    assert room.status.value == "GAME_SETUP"
    assert room.engine is not first_engine


def test_cancel_setup_enforces_host_and_phase_rules():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])

        sockets[1].send_json({"action": "CANCEL_GAME_SETUP", "payload": {}})
        non_host_error = receive_type(sockets[1], "ERROR")

        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
        )
        receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SELECT_FIRST_PLAYER",
        )
        sockets[0].send_json({"action": "CANCEL_GAME_SETUP", "payload": {}})
        phase_error = receive_type(sockets[0], "ERROR")

    assert non_host_error["payload"]["code"] == "HOST_ONLY"
    assert phase_error["payload"]["code"] == "INVALID_PHASE"


def test_duplicate_start_does_not_replace_the_setup_engine():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        engine = routes.room_manager.get_room(room_id).engine

        sockets[0].send_json({"action": "START_GAME", "payload": {}})
        error = receive_type(sockets[0], "ERROR")

    room = routes.room_manager.get_room(room_id)
    assert error["payload"]["code"] == "GAME_ALREADY_STARTED"
    assert room.engine is engine
    assert room.status.value == "GAME_SETUP"
    assert room.engine.state.phase == GamePhase.FIRST_PLAYER_SELECTION


def test_reconnect_during_setup_receives_current_authoritative_phase():
    room_id, players = create_joined_room()
    player_url = f"/ws/rooms/{room_id}?token={players[2]['session_token']}"

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[2].close()

        with client.websocket_connect(player_url) as replacement:
            room_state = receive_type(
                replacement,
                "ROOM_STATE_UPDATE",
                lambda message: message["payload"]["status"] == "GAME_SETUP",
            )
            game_state = receive_type(replacement, "GAME_STATE_UPDATE", setup_state)

    assert room_state["payload"]["host_id"] == "P0"
    assert game_state["payload"]["room_status"] == "GAME_SETUP"


def test_host_reconnect_during_setup_preserves_setup_authority():
    room_id, players = create_joined_room()
    host_url = f"/ws/rooms/{room_id}?token={players[0]['session_token']}"

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].close()

        with client.websocket_connect(host_url) as replacement:
            receive_type(replacement, "ROOM_STATE_UPDATE")
            state = receive_type(replacement, "GAME_STATE_UPDATE", setup_state)
            replacement.send_json(
                {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
            )
            success = receive_type(replacement, "ACTION_SUCCESS")

    assert state["payload"]["host_id"] == "P0"
    assert success["payload"]["action"] == "SELECT_FIRST_PLAYER"
    assert routes.room_manager.get_room(room_id).status.value == "IN_GAME"


def test_host_timeout_transfers_setup_authority_to_next_host(monkeypatch):
    monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].close()
        time.sleep(0.04)

        updated_room = receive_type(
            sockets[1],
            "ROOM_STATE_UPDATE",
            lambda message: (
                message["payload"]["status"] == "GAME_SETUP"
                and message["payload"]["host_id"] == "P1"
            ),
        )["payload"]
        updated_game = receive_type(
            sockets[1], "GAME_STATE_UPDATE", lambda message: setup_state(message, "P1")
        )["payload"]

        sockets[1].send_json({"action": "CANCEL_GAME_SETUP", "payload": {}})
        receive_type(
            sockets[1],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "CANCEL_GAME_SETUP",
        )

    assert updated_room["host_id"] == "P1"
    assert updated_game["host_id"] == "P1"
    assert [player["player_id"] for player in updated_game["players"]] == [
        "P1",
        "P2",
        "P3",
    ]


def test_normal_first_player_selection_deals_advances_everyone_and_is_safe_once():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])

        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "missing"}}
        )
        invalid_target = receive_type(sockets[0], "ERROR")

        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P2"}}
        )
        receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SELECT_FIRST_PLAYER",
        )
        states = [
            receive_type(
                socket,
                "GAME_STATE_UPDATE",
                lambda message: (
                    message["payload"]["phase"] == GamePhase.PLAYING.value
                    and message["payload"]["selected_first_player_id"] == "P2"
                    and message["payload"]["current_player_id"] == "P2"
                ),
            )["payload"]
            for socket in sockets
        ]

        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P3"}}
        )
        duplicate = receive_type(sockets[0], "ERROR")

    assert invalid_target["payload"]["code"] == "PLAYER_NOT_FOUND"
    assert duplicate["payload"]["code"] == "INVALID_PHASE"
    assert all(state["room_status"] == "IN_GAME" for state in states)
    assert all(state["phase"] == GamePhase.PLAYING.value for state in states)
    assert routes.room_manager.get_room(room_id).status.value == "IN_GAME"


def test_hidden_first_player_selection_deals_to_hidden_trump_setup():
    room_id, players = create_joined_room(trump_mode="hidden")

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].send_json(
            {"action": "SELECT_TRUMP_HIDER", "payload": {"player_id": "P1"}}
        )
        receive_type(sockets[0], "ACTION_SUCCESS")
        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P2"}}
        )
        receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SELECT_FIRST_PLAYER",
        )
        states = [
            receive_type(
                socket,
                "GAME_STATE_UPDATE",
                lambda message: (
                    message["payload"]["phase"]
                    == GamePhase.HIDDEN_TRUMP_SELECTION.value
                    and message["payload"]["selected_first_player_id"] == "P2"
                ),
            )["payload"]
            for socket in sockets
        ]

    assert all(state["room_status"] == "IN_GAME" for state in states)
    assert all(state["trump_state"]["trump_hider_id"] == "P1" for state in states)
    assert all(state["current_player_id"] is None for state in states)


@pytest.mark.parametrize(("player_count", "cards_per_player"), [(4, 12), (6, 8), (8, 6)])
def test_first_player_selection_deals_correct_card_count(player_count, cards_per_player):
    room_id, players = create_joined_room(player_count=player_count)

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
        )
        receive_type(sockets[0], "ACTION_SUCCESS")

    room = routes.room_manager.get_room(room_id)
    assert room.engine.state.phase == GamePhase.PLAYING
    assert all(
        len(room.engine.state.hands[player["player_id"]]) == cards_per_player
        for player in players
    )


def test_duplicate_first_player_selection_does_not_deal_twice():
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P2"}}
        )
        receive_type(sockets[0], "ACTION_SUCCESS")
        room = routes.room_manager.get_room(room_id)
        hands_before = {
            player_id: list(hand) for player_id, hand in room.engine.state.hands.items()
        }
        version_before = room.engine.state.version

        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P3"}}
        )
        duplicate = receive_type(sockets[0], "ERROR")

    assert duplicate["payload"]["code"] == "INVALID_PHASE"
    assert room.engine.state.selected_first_player_id == "P2"
    assert room.engine.state.hands == hands_before
    assert room.engine.state.version == version_before


def test_reconnect_after_first_player_selection_receives_actual_playing_phase():
    room_id, players = create_joined_room()
    reconnect_url = f"/ws/rooms/{room_id}?token={players[2]['session_token']}"

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
        )
        receive_type(sockets[0], "ACTION_SUCCESS")
        sockets[2].close()

        with client.websocket_connect(reconnect_url) as replacement:
            receive_type(
                replacement,
                "ROOM_STATE_UPDATE",
                lambda message: message["payload"]["status"] == "IN_GAME",
            )
            state = receive_type(
                replacement,
                "GAME_STATE_UPDATE",
                lambda message: message["payload"]["phase"] == GamePhase.PLAYING.value,
            )["payload"]

    assert state["room_status"] == "IN_GAME"
    assert state["current_player_id"] == "P0"


def test_dealing_failure_restores_recoverable_setup_state(monkeypatch):
    room_id, players = create_joined_room()

    with ExitStack() as stack:
        sockets = connect_all(stack, room_id, players)
        start_setup(sockets[0])
        room = routes.room_manager.get_room(room_id)

        def fail_dealing():
            raise InvalidPhase("forced deal failure")

        monkeypatch.setattr(room.engine, "deal_cards", fail_dealing)
        sockets[0].send_json(
            {"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": "P0"}}
        )
        error = receive_type(sockets[0], "ERROR")

    assert error["payload"]["code"] == "INVALID_PHASE"
    assert room.status.value == "GAME_SETUP"
    assert room.engine.state.phase == GamePhase.FIRST_PLAYER_SELECTION
    assert room.engine.state.selected_first_player_id is None
    assert not room.engine.state.hands
