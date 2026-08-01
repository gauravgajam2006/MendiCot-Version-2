import asyncio
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from mendicot.api import routes


client = TestClient(routes.app)


def _clear_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


@pytest.fixture(autouse=True)
def reset_state():
    _clear_state()
    yield
    _clear_state()


def create_and_join(player_count=4, joined_count=None):
    response = client.post(
        "/api/rooms",
        json={"player_count": player_count, "trump_mode": "normal"},
    )
    assert response.status_code == 200, response.text
    room_id = response.json()["room_id"]
    joined_count = player_count if joined_count is None else joined_count
    players = []
    for index in range(joined_count):
        response = client.post(
            f"/api/rooms/{room_id}/join",
            json={
                "player_id": f"P{index}",
                "display_name": f"Player {index}",
            },
        )
        assert response.status_code == 200, response.text
        players.append(response.json())
    return room_id, players


def connect(stack, room_id, players, indices=None):
    indices = range(len(players)) if indices is None else indices
    return {
        index: stack.enter_context(
            client.websocket_connect(
                f"/ws/rooms/{room_id}?token={players[index]['session_token']}"
            )
        )
        for index in indices
    }


def receive_type(websocket, message_type, predicate=None, max_messages=50):
    for _ in range(max_messages):
        message = websocket.receive_json()
        if message["type"] == message_type and (
            predicate is None or predicate(message)
        ):
            return message
    raise AssertionError(f"Did not receive {message_type}")


def send_action(websocket, action, payload=None):
    websocket.send_json(
        {"action": action, "payload": {} if payload is None else payload}
    )


def by_player(state):
    return {player["player_id"]: player for player in state["players"]}


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_join_assigns_alternating_persistent_teams_and_stable_seats(player_count):
    room_id, _ = create_and_join(player_count)

    first_state = routes.room_manager.get_room(room_id).get_state()
    second_state = routes.room_manager.get_room(room_id).get_state()
    expected_teams = ["TeamA" if index % 2 == 0 else "TeamB" for index in range(player_count)]

    assert [player["player_id"] for player in first_state["players"]] == [
        f"P{index}" for index in range(player_count)
    ]
    assert [player["team_id"] for player in first_state["players"]] == expected_teams
    assert [player["seat_index"] for player in first_state["players"]] == list(
        range(player_count)
    )
    assert second_state["players"] == first_state["players"]


def test_replacement_join_reuses_free_seat_but_continues_team_sequence():
    room_id, _ = create_and_join(4)
    room = routes.room_manager.get_room(room_id)
    room.remove_player("P1")

    response = client.post(
        f"/api/rooms/{room_id}/join",
        json={"player_id": "P4", "display_name": "Player 4"},
    )
    assert response.status_code == 200, response.text

    state = room.get_state()
    replacement = by_player(state)["P4"]
    assert replacement["team_id"] == "TeamA"
    assert replacement["seat_index"] == 1
    assert len({player["seat_index"] for player in state["players"]}) == 4


def test_room_state_player_shape_includes_authoritative_team_and_seat():
    room_id, players = create_and_join(4, joined_count=1)

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={players[0]['session_token']}"
    ) as websocket:
        state = receive_type(websocket, "ROOM_STATE_UPDATE")["payload"]

    assert by_player(state)["P0"] == {
        "player_id": "P0",
        "display_name": "Player 0",
        "team_id": "TeamA",
        "seat_index": 0,
        "is_online": True,
    }


def test_switch_team_changes_only_authenticated_actor_and_all_clients_agree():
    room_id, players = create_and_join(4)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players, indices=(1, 0))
        actor = sockets[1]
        observer = sockets[0]
        send_action(
            actor,
            "SWITCH_TEAM",
            {"team_id": "TeamA", "player_id": "P3"},
        )

        success = receive_type(actor, "ACTION_SUCCESS")
        actor_state = receive_type(
            actor,
            "ROOM_STATE_UPDATE",
            lambda message: by_player(message["payload"])["P1"]["team_id"]
            == "TeamA",
        )["payload"]
        observer_state = receive_type(
            observer,
            "ROOM_STATE_UPDATE",
            lambda message: by_player(message["payload"])["P1"]["team_id"]
            == "TeamA",
        )["payload"]

    assert success["payload"]["action"] == "SWITCH_TEAM"
    assert actor_state["players"] == observer_state["players"]
    assert by_player(actor_state)["P1"]["team_id"] == "TeamA"
    assert by_player(actor_state)["P3"]["team_id"] == "TeamB"
    assert by_player(actor_state)["P1"]["seat_index"] == 1
    assert actor_state["host_id"] == "P0"
    assert [player["team_id"] for player in actor_state["players"]].count("TeamA") == 3


def test_host_can_switch_team_without_losing_host_or_player_metadata():
    room_id, players = create_and_join(4, joined_count=2)

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={players[0]['session_token']}"
    ) as host:
        send_action(host, "SWITCH_TEAM", {"team_id": "TeamB"})
        assert receive_type(host, "ACTION_SUCCESS")["payload"]["action"] == "SWITCH_TEAM"
        state = receive_type(
            host,
            "ROOM_STATE_UPDATE",
            lambda message: by_player(message["payload"])["P0"]["team_id"]
            == "TeamB",
        )["payload"]

    host_player = by_player(state)["P0"]
    assert state["host_id"] == "P0"
    assert host_player == {
        "player_id": "P0",
        "display_name": "Player 0",
        "team_id": "TeamB",
        "seat_index": 0,
        "is_online": True,
    }


def test_invalid_team_is_recoverable_and_does_not_mutate_player():
    room_id, players = create_and_join(4, joined_count=1)

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={players[0]['session_token']}"
    ) as websocket:
        send_action(websocket, "SWITCH_TEAM", {"team_id": "NotATeam"})
        error = receive_type(websocket, "ERROR")

    assert error["payload"]["action"] == "SWITCH_TEAM"
    assert error["payload"]["code"] == "INVALID_TEAM"
    assert error["payload"]["message"]
    assert routes.room_manager.get_room(room_id).get_player("P0").team_id == "TeamA"


def test_same_team_switch_is_idempotent_and_broadcasts_state():
    room_id, players = create_and_join(4, joined_count=1)

    with client.websocket_connect(
        f"/ws/rooms/{room_id}?token={players[0]['session_token']}"
    ) as websocket:
        send_action(websocket, "SWITCH_TEAM", {"team_id": "TeamA"})
        assert receive_type(websocket, "ACTION_SUCCESS")["payload"]["action"] == "SWITCH_TEAM"
        state = receive_type(websocket, "ROOM_STATE_UPDATE")["payload"]

    assert by_player(state)["P0"]["team_id"] == "TeamA"
    assert by_player(state)["P0"]["seat_index"] == 0


def test_switch_and_second_start_are_rejected_after_game_starts():
    room_id, players = create_and_join(4)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players)
        host = sockets[0]
        send_action(host, "START_GAME")
        assert receive_type(
            host,
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "START_GAME",
        )

        send_action(host, "SWITCH_TEAM", {"team_id": "TeamB"})
        switch_error = receive_type(
            host,
            "ERROR",
            lambda message: message["payload"]["action"] == "SWITCH_TEAM",
        )
        send_action(host, "START_GAME")
        start_error = receive_type(
            host,
            "ERROR",
            lambda message: message["payload"]["action"] == "START_GAME",
        )

    assert switch_error["payload"]["code"] == "GAME_ALREADY_STARTED"
    assert start_error["payload"]["code"] == "GAME_ALREADY_STARTED"
    assert routes.room_manager.get_room(room_id).get_player("P0").team_id == "TeamA"


@pytest.mark.parametrize(
    ("joined_count", "connected_indices", "actor_index", "expected_code"),
    [
        (3, (0,), 0, "ROOM_NOT_FULL"),
        (4, (0,), 0, "PLAYER_OFFLINE"),
        (4, (1,), 1, "HOST_ONLY"),
    ],
    ids=("room-not-full", "offline-player", "non-host"),
)
def test_start_rejects_lobby_preconditions(
    joined_count, connected_indices, actor_index, expected_code
):
    room_id, players = create_and_join(4, joined_count=joined_count)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players, connected_indices)
        send_action(sockets[actor_index], "START_GAME")
        error = receive_type(sockets[actor_index], "ERROR")

    assert error["payload"]["action"] == "START_GAME"
    assert error["payload"]["code"] == expected_code
    room = routes.room_manager.get_room(room_id)
    assert room.status.value == "WAITING"
    assert room.engine is None


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_unbalanced_full_online_room_cannot_start(player_count):
    room_id, players = create_and_join(player_count)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players)
        host = sockets[0]
        send_action(host, "SWITCH_TEAM", {"team_id": "TeamB"})
        assert receive_type(
            host,
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SWITCH_TEAM",
        )
        send_action(host, "START_GAME")
        error = receive_type(
            host,
            "ERROR",
            lambda message: message["payload"]["action"] == "START_GAME",
        )

    assert error["payload"]["code"] == "TEAMS_UNBALANCED"
    state = routes.room_manager.get_room(room_id).get_state()
    team_a_count = sum(player["team_id"] == "TeamA" for player in state["players"])
    assert team_a_count == player_count // 2 - 1
    assert routes.room_manager.get_room(room_id).engine is None


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_balanced_full_online_room_starts_successfully(player_count):
    room_id, players = create_and_join(player_count)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players)
        host = sockets[0]
        send_action(host, "START_GAME")
        success = receive_type(
            host,
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "START_GAME",
        )

    room = routes.room_manager.get_room(room_id)
    assert success["payload"]["action"] == "START_GAME"
    assert room.status.value == "IN_GAME"
    assert room.engine.state.player_count == player_count
    assert {
        team_id: len(team.player_ids)
        for team_id, team in room.engine.state.teams.items()
    } == {"TeamA": player_count // 2, "TeamB": player_count // 2}


def test_persisted_team_ids_drive_deterministic_alternating_engine_seats():
    room_id, players = create_and_join(4)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players)
        send_action(sockets[0], "SWITCH_TEAM", {"team_id": "TeamB"})
        receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SWITCH_TEAM",
        )
        send_action(sockets[1], "SWITCH_TEAM", {"team_id": "TeamA"})
        receive_type(
            sockets[1],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "SWITCH_TEAM",
        )

        lobby_state = routes.room_manager.get_room(room_id).get_state()
        send_action(sockets[0], "START_GAME")
        receive_type(
            sockets[0],
            "ACTION_SUCCESS",
            lambda message: message["payload"]["action"] == "START_GAME",
        )

    assert [
        (player["player_id"], player["team_id"], player["seat_index"])
        for player in lobby_state["players"]
    ] == [
        ("P0", "TeamB", 0),
        ("P1", "TeamA", 1),
        ("P2", "TeamA", 2),
        ("P3", "TeamB", 3),
    ]

    engine_state = routes.room_manager.get_room(room_id).engine.state
    assert [
        (player.player_id, player.team_id, player.seat_index)
        for player in engine_state.players
    ] == [
        ("P0", "TeamB", 0),
        ("P1", "TeamA", 1),
        ("P3", "TeamB", 2),
        ("P2", "TeamA", 3),
    ]
    assert engine_state.seat_order == ["P0", "P1", "P3", "P2"]
    assert engine_state.teams["TeamA"].player_ids == ["P1", "P2"]
    assert engine_state.teams["TeamB"].player_ids == ["P0", "P3"]


def test_reconnect_and_session_resume_preserve_team_seat_and_host(monkeypatch):
    monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 1.0)
    room_id, players = create_and_join(4, joined_count=1)
    websocket_url = f"/ws/rooms/{room_id}?token={players[0]['session_token']}"

    with client.websocket_connect(websocket_url) as websocket:
        send_action(websocket, "SWITCH_TEAM", {"team_id": "TeamB"})
        receive_type(websocket, "ACTION_SUCCESS")
        receive_type(
            websocket,
            "ROOM_STATE_UPDATE",
            lambda message: by_player(message["payload"])["P0"]["team_id"]
            == "TeamB",
        )

    validation = client.post(
        f"/api/rooms/{room_id}/sessions/validate",
        json={
            "player_id": "P0",
            "session_token": players[0]["session_token"],
        },
    )
    assert validation.status_code == 200, validation.text

    with client.websocket_connect(websocket_url) as replacement:
        resumed_state = receive_type(replacement, "ROOM_STATE_UPDATE")["payload"]

    assert resumed_state["host_id"] == "P0"
    assert by_player(resumed_state)["P0"] == {
        "player_id": "P0",
        "display_name": "Player 0",
        "team_id": "TeamB",
        "seat_index": 0,
        "is_online": True,
    }


class _Socket:
    async def accept(self):
        pass

    async def send_json(self, message):
        pass


def test_timeout_removal_removes_player_without_changing_remaining_teams(monkeypatch):
    monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)
    room_id, players = create_and_join(4, joined_count=3)
    room = routes.room_manager.get_room(room_id)
    before = {
        player["player_id"]: (player["team_id"], player["seat_index"])
        for player in room.get_state()["players"]
        if player["player_id"] != "P1"
    }

    async def expire_player():
        socket = _Socket()
        await routes.connection_manager.connect(room_id, "P1", socket)
        room.set_player_online("P1", True)
        await routes._handle_socket_disconnect(room_id, "P1", socket)
        await asyncio.sleep(0.04)

    asyncio.run(expire_player())

    remaining = {
        player["player_id"]: (player["team_id"], player["seat_index"])
        for player in room.get_state()["players"]
    }
    assert remaining == before
    assert "P1" not in room.player_ids
    assert players[1]["session_token"] not in routes.session_tokens


def test_intentional_host_leave_transfers_host_and_preserves_remaining_teams():
    room_id, players = create_and_join(4, joined_count=3)

    with ExitStack() as stack:
        sockets = connect(stack, room_id, players, indices=(1, 0))
        watcher = sockets[1]
        host = sockets[0]
        send_action(host, "SWITCH_TEAM", {"team_id": "TeamB"})
        receive_type(host, "ACTION_SUCCESS")
        before = {
            player["player_id"]: (player["team_id"], player["seat_index"])
            for player in routes.room_manager.get_room(room_id).get_state()["players"]
            if player["player_id"] != "P0"
        }

        send_action(host, "LEAVE_ROOM")
        assert receive_type(host, "ACTION_SUCCESS")["payload"]["action"] == "LEAVE_ROOM"
        state = receive_type(
            watcher,
            "ROOM_STATE_UPDATE",
            lambda message: message["payload"]["host_id"] == "P1"
            and "P0" not in by_player(message["payload"]),
        )["payload"]

        after = {
            player["player_id"]: (player["team_id"], player["seat_index"])
            for player in state["players"]
        }
        assert state["host_id"] == "P1"
        assert after == before
        assert "P0" not in routes.room_manager.get_room(room_id).player_ids
        assert players[0]["session_token"] not in routes.session_tokens
