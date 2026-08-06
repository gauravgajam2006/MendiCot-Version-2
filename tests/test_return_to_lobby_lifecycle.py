import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.websockets import WebSocketDisconnect

from mendicot.api import routes
from mendicot.enums import GamePhase, Rank, Suit
from mendicot.exceptions import (
    GameAlreadyStarted,
    GameNotStarted,
    InvalidPhase,
    PlayerNotInRoom,
)
from mendicot.models import Card
from mendicot.room import RoomStatus


class Socket:
    def __init__(self):
        self.messages = []
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class FakeTask:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


def clear_state():
    for registry in (
        routes._disconnect_cleanup_tasks,
        routes._trick_resolution_tasks,
        routes._final_score_display_tasks,
        routes._trump_reveal_display_tasks,
        routes._hidden_card_return_tasks,
    ):
        for task in list(registry.values()):
            try:
                task.cancel()
            except RuntimeError:
                pass
        registry.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


async def _drain_disconnect_cleanups():
    for key, task in list(routes._disconnect_cleanup_tasks.items()):
        task.cancel()
    pending = list(routes._disconnect_cleanup_tasks.values())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    routes._disconnect_cleanup_tasks.clear()


@pytest.fixture(autouse=True)
def reset_state():
    clear_state()
    yield
    clear_state()


def _create_room(room_id, player_count, trump_mode="normal"):
    room = routes.room_manager.create_room(room_id, player_count, trump_mode)
    for index in range(player_count):
        player_id = f"P{index + 1}"
        room.add_player(player_id, f"Player {index + 1}")
        room.set_player_online(player_id, True)
    return room


def _start_ingame_room(room_id, player_count, trump_mode="normal"):
    room = _create_room(room_id, player_count, trump_mode)
    room.start_game("P1", set(room.player_ids))
    if trump_mode == "hidden":
        room.select_trump_hider("P1", "P1")
    room.select_first_player("P1", "P1")
    assert room.status == RoomStatus.IN_GAME
    return room


def _drive_to_terminal(room, draw=False):
    state = room.engine.state
    if draw:
        state.captured_mendis["TeamA"] = [Suit.SPADES]
        state.teams["TeamA"].tens_captured = 1
        state.teams["TeamA"].tricks_won = 1
    state.phase = GamePhase.PLAYING
    state.current_turn = "P1"
    state.hands = {
        player.player_id: [
            Card(
                Suit.HEARTS,
                Rank.TEN if player.player_id == "P1" else Rank.ACE
                if player.player_id == "P2"
                else Rank.THREE,
            )
        ]
        for player in state.players
    }
    for _ in range(state.player_count):
        player_id = state.current_turn
        room.engine.play_card(player_id, state.hands[player_id][0])
    assert state.phase == GamePhase.TRICK_RESOLUTION
    room.engine.resolve_trick()
    assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
    room.engine._deal_audit = None
    room.engine.finalize_game()
    assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)
    return state.phase


def _assert_terminal_intact(room, phase):
    assert room.status == RoomStatus.IN_GAME
    assert room.engine is not None
    assert room.engine.state.phase == phase
    assert room.engine.state.final_result in ("TeamA", "TeamB", "DRAW")


def _return_all(room, player_ids):
    for player_id in player_ids[:-1]:
        assert room.return_to_lobby(player_id) is False
    assert room.return_to_lobby(player_ids[-1]) is True
    assert room.status == RoomStatus.WAITING
    assert room.engine is None


async def _connect_sockets(room):
    sockets = {}
    for player_id in room.player_ids:
        socket = Socket()
        await routes.connection_manager.connect(room.room_id, player_id, socket)
        routes.session_tokens[f"token-{player_id}"] = {
            "room_id": room.room_id,
            "player_id": player_id,
        }
        sockets[player_id] = socket
    return sockets


# ---------------------------------------------------------------------------
# WebSocket endpoint driver (real message dispatch)
# ---------------------------------------------------------------------------

_DISCONNECT = object()


class EndpointSocket:
    def __init__(self):
        self.accepted = False
        self.sent = []
        self.closed = None
        self._inbox = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def send_json(self, message):
        self.sent.append(message)

    async def receive_json(self):
        message = await self._inbox.get()
        if message is _DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        return message

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def send(self, message):
        self._inbox.put_nowait(message)

    def disconnect(self):
        self._inbox.put_nowait(_DISCONNECT)


async def _drive_endpoint(socket, room_id, token):
    try:
        await routes.websocket_endpoint(socket, room_id, token=token)
    except Exception:
        pass


async def _wait_for(socket, message_type, start=0, predicate=None, timeout=2.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for index in range(start, len(socket.sent)):
            message = socket.sent[index]
            if message["type"] == message_type and (
                predicate is None or predicate(message)
            ):
                return index, message
        await asyncio.sleep(0.001)
    raise AssertionError(
        f"Did not receive {message_type}. Received: {socket.sent}"
    )


def _return_success(action="RETURN_TO_LOBBY"):
    return lambda message: message["payload"]["action"] == action


async def _start_clients(room):
    clients = {}
    for player_id in room.player_ids:
        token = f"token-{player_id}"
        routes.session_tokens[token] = {
            "room_id": room.room_id,
            "player_id": player_id,
        }
        socket = EndpointSocket()
        clients[player_id] = (
            socket,
            asyncio.create_task(
                _drive_endpoint(socket, room.room_id, token=token)
            ),
        )
    for player_id, (socket, _) in clients.items():
        await _wait_for(socket, "ROOM_STATE_UPDATE")
    return clients


async def _terminate_clients(clients):
    for _, (_, task) in clients.items():
        if task is not None:
            task.cancel()
    for _, (_, task) in clients.items():
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await _drain_disconnect_cleanups()


async def _disconnect_client(clients, player_id):
    _, task = clients[player_id]
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    clients[player_id] = (clients[player_id][0], None)
    await asyncio.sleep(0)


async def _connect_replacement(clients, room, player_id):
    socket = EndpointSocket()
    task = asyncio.create_task(
        _drive_endpoint(socket, room.room_id, token=f"token-{player_id}")
    )
    clients[player_id] = (socket, task)
    await _wait_for(socket, "ROOM_STATE_UPDATE")
    return socket, task


# ---------------------------------------------------------------------------
# 1. Individual return
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
@pytest.mark.parametrize("draw", [False, True])
def test_non_host_returns_alone_marks_only_requester(
    player_count, trump_mode, draw
):
    async def scenario():
        room = _start_ingame_room(
            f"one-{player_count}-{trump_mode}-{draw}", player_count, trump_mode
        )
        phase = _drive_to_terminal(room, draw=draw)

        was_reset = room.return_to_lobby("P2")

        assert was_reset is False
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None
        assert room.engine.state.phase == phase
        assert set(room.returned_to_lobby_player_ids) == {"P2"}
        assert room.host_id == "P1"

    asyncio.run(scenario())


def test_host_returns_alone_preserves_host_and_terminal_room():
    async def scenario():
        room = _start_ingame_room("host-alone", 4)
        phase = _drive_to_terminal(room)

        was_reset = room.return_to_lobby("P1")

        assert was_reset is False
        assert room.host_id == "P1"
        assert set(room.returned_to_lobby_player_ids) == {"P1"}
        _assert_terminal_intact(room, phase)

    asyncio.run(scenario())


def test_partial_returns_only_add_requester_and_keep_others_waiting():
    async def scenario():
        room = _start_ingame_room("only-requester", 4)
        _drive_to_terminal(room)

        room.return_to_lobby("P3")

        assert set(room.returned_to_lobby_player_ids) == {"P3"}
        assert "P1" not in room.returned_to_lobby_player_ids
        assert "P2" not in room.returned_to_lobby_player_ids
        assert "P4" not in room.returned_to_lobby_player_ids

    asyncio.run(scenario())


def test_terminal_result_scores_and_mendis_survive_partial_return():
    async def scenario():
        room = _start_ingame_room("result-preserve", 4)
        phase = _drive_to_terminal(room)
        engine = room.engine
        final_result = engine.state.final_result
        scores = engine.calculate_score()
        mendis = {k: list(v) for k, v in engine.state.captured_mendis.items()}
        completed_tricks = list(engine.state.completed_tricks)
        version = engine.state.version

        room.return_to_lobby("P3")
        room.return_to_lobby("P2")

        assert room.engine is engine
        assert room.engine.state.phase == phase
        assert room.engine.state.final_result == final_result
        assert room.engine.calculate_score() == scores
        assert {
            k: list(v) for k, v in room.engine.state.captured_mendis.items()
        } == mendis
        assert list(room.engine.state.completed_tricks) == completed_tricks
        assert room.engine.state.version == version
        assert room.status == RoomStatus.IN_GAME

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. Status serialization
# ---------------------------------------------------------------------------

def test_room_state_snapshot_exposes_returned_set_in_seat_order():
    async def scenario():
        room = _start_ingame_room("snapshot-room", 4)
        _drive_to_terminal(room)

        room.return_to_lobby("P4")
        room.return_to_lobby("P2")

        state = room.get_state()
        assert state["status"] == "IN_GAME"
        assert state["returned_to_lobby_player_ids"] == ["P2", "P4"]
        assert "P1" not in state["returned_to_lobby_player_ids"]
        assert "P3" not in state["returned_to_lobby_player_ids"]
        assert all(player["is_online"] for player in state["players"])

    asyncio.run(scenario())


def test_ws_snapshot_exposes_returned_set_and_online_state():
    async def scenario():
        room = _start_ingame_room("ws-snapshot", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        _, room_state = await _wait_for(
            clients["P1"][0],
            "ROOM_STATE_UPDATE",
            predicate=lambda m: m["payload"].get("returned_to_lobby_player_ids")
            == ["P2"],
        )
        assert room_state["payload"]["status"] == "IN_GAME"
        assert room_state["payload"]["players"][1]["player_id"] == "P2"
        assert room_state["payload"]["players"][1]["is_online"] is True
        assert room_state["payload"]["players"][0]["is_online"] is True

        _, game_state = await _wait_for(
            clients["P1"][0],
            "GAME_STATE_UPDATE",
            predicate=lambda m: m["payload"].get("returned_to_lobby_player_ids")
            == ["P2"],
        )
        assert game_state["payload"]["phase"] == "GAME_OVER"
        assert game_state["payload"]["room_status"] == "IN_GAME"
        assert game_state["payload"]["final_result"] in ("TeamA", "TeamB", "DRAW")

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_returned_player_reconnect_snapshot_routes_to_lobby(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 10)
        room = _start_ingame_room("reconnect-route", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        await _disconnect_client(clients, "P2")
        assert "P2" in room.returned_to_lobby_player_ids

        await _connect_replacement(clients, room, "P2")
        _, game_state = await _wait_for(
            clients["P2"][0],
            "GAME_STATE_UPDATE",
            predicate=lambda m: m["payload"].get("returned_to_lobby_player_ids")
            == ["P2"],
        )
        assert game_state["payload"]["room_status"] == "IN_GAME"
        assert game_state["payload"]["phase"] == "GAME_OVER"
        assert "P2" in game_state["payload"]["returned_to_lobby_player_ids"]

        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. Mixed post-game state
# ---------------------------------------------------------------------------

def test_join_team_switch_rename_and_start_disabled_during_mixed_state():
    async def scenario():
        room = _start_ingame_room("mixed-disabled", 4)
        _drive_to_terminal(room)
        room.return_to_lobby("P2")

        assert room.status == RoomStatus.IN_GAME
        with pytest.raises(GameAlreadyStarted):
            room.start_game("P1", set(room.player_ids))
        with pytest.raises(GameAlreadyStarted):
            room.switch_team("P2", "TeamA")
        with pytest.raises(InvalidPhase):
            room.rename_team("P2", "New Name")

        room.remove_player("P3")
        with pytest.raises(GameAlreadyStarted):
            room.add_player("P9")

    asyncio.run(scenario())


def test_partial_return_keeps_all_sessions_and_websockets():
    async def scenario():
        room = _start_ingame_room("keep-sockets", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        connected = routes.connection_manager.get_connected_player_ids(room.room_id)
        assert connected == set(room.player_ids)
        for player_id in room.player_ids:
            assert (
                routes.session_tokens[f"token-{player_id}"]["player_id"] == player_id
            )
        assert room.status == RoomStatus.IN_GAME

        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. Multiple returns and final reset
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
@pytest.mark.parametrize("draw", [False, True])
def test_final_player_return_resets_room_to_waiting(
    player_count, trump_mode, draw
):
    async def scenario():
        room = _start_ingame_room(
            f"final-{player_count}-{trump_mode}-{draw}", player_count, trump_mode
        )
        phase = _drive_to_terminal(room, draw=draw)

        returned_ids = room.player_ids[:-1]
        for player_id in returned_ids:
            assert room.return_to_lobby(player_id) is False
        _assert_terminal_intact(room, phase)

        generation_before = room.match_generation
        last = room.player_ids[-1]
        assert room.return_to_lobby(last) is True

        assert room.status == RoomStatus.WAITING
        assert room.engine is None
        assert room.returned_to_lobby_player_ids == set()
        assert room.match_generation == generation_before + 1
        assert room.host_id == "P1"
        assert set(room.player_ids) == {f"P{i}" for i in range(1, player_count + 1)}

    asyncio.run(scenario())


def test_partial_returns_1_2_3_of_4_stay_ingame():
    async def scenario():
        room = _start_ingame_room("partial-counts", 4)
        _drive_to_terminal(room)

        for returned in ("P2", "P4", "P3"):
            assert room.return_to_lobby(returned) is False
            assert room.status == RoomStatus.IN_GAME
            assert room.engine is not None

        assert set(room.returned_to_lobby_player_ids) == {"P2", "P4", "P3"}

    asyncio.run(scenario())


def test_final_reset_preserves_room_identity_players_host_and_team_names():
    async def scenario():
        room = _create_room("preserve", 4)
        room.rename_team("P1", "Crimson")
        room.rename_team("P2", "Amber")
        room.start_game("P1", set(room.player_ids))
        room.select_first_player("P1", "P1")
        _drive_to_terminal(room)

        original_players = [
            (player.player_id, player.team_id.value, player.seat_index)
            for player in sorted(room._players, key=lambda p: p.seat_index)
        ]
        original_host = room.host_id
        original_room_id = room.room_id

        _return_all(room, room.player_ids)

        assert room.room_id == original_room_id
        assert room.host_id == original_host
        assert [
            (p.player_id, p.team_id.value, p.seat_index)
            for p in sorted(room._players, key=lambda p: p.seat_index)
        ] == original_players
        state = room.get_state()
        assert "Crimson" in state["team_names"].values()
        assert "Amber" in state["team_names"].values()

    asyncio.run(scenario())


def test_final_return_broadcasts_one_waiting_and_no_stale_game_update():
    async def scenario():
        room = _start_ingame_room("one-waiting", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        for player_id in ("P1", "P2", "P3"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )

        requester = clients["P4"][0]
        requester.send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(requester, "ACTION_SUCCESS", predicate=_return_success())

        for player_id, (socket, _) in clients.items():
            await _wait_for(
                socket,
                "ROOM_STATE_UPDATE",
                predicate=lambda m: m["payload"]["status"] == "WAITING",
            )
            waiting = [
                message
                for message in socket.sent
                if message["type"] == "ROOM_STATE_UPDATE"
                and message["payload"]["status"] == "WAITING"
            ]
            assert len(waiting) == 1, player_id
            lobby_index = socket.sent.index(waiting[0])
            stale_after = [
                message
                for message in socket.sent[lobby_index + 1:]
                if message["type"] == "GAME_STATE_UPDATE"
            ]
            assert not stale_after, player_id

        assert room.engine is None
        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Leave behavior
# ---------------------------------------------------------------------------

def test_returned_player_leave_removes_from_room_and_set():
    async def scenario():
        room = _start_ingame_room("returned-leave", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        clients["P2"][0].send({"action": "LEAVE_ROOM", "payload": {}})
        await _wait_for(
            clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success("LEAVE_ROOM")
        )

        assert "P2" not in room.player_ids
        assert "P2" not in room.returned_to_lobby_player_ids
        assert "token-P2" not in routes.session_tokens
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_non_returned_player_leave_keeps_terminal_room():
    async def scenario():
        room = _start_ingame_room("non-returned-leave", 4)
        phase = _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        clients["P3"][0].send({"action": "LEAVE_ROOM", "payload": {}})
        await _wait_for(
            clients["P3"][0], "ACTION_SUCCESS", predicate=_return_success("LEAVE_ROOM")
        )

        assert "P3" not in room.player_ids
        assert room.status == RoomStatus.IN_GAME
        assert room.engine.state.phase == phase
        assert set(room.returned_to_lobby_player_ids) == {"P2"}

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_host_leave_during_mixed_state_transfers_host():
    async def scenario():
        room = _start_ingame_room("host-leave-mixed", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P1"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P1"][0], "ACTION_SUCCESS", predicate=_return_success())

        clients["P1"][0].send({"action": "LEAVE_ROOM", "payload": {}})
        await _wait_for(
            clients["P1"][0], "ACTION_SUCCESS", predicate=_return_success("LEAVE_ROOM")
        )

        assert "P1" not in room.player_ids
        assert "P1" not in room.returned_to_lobby_player_ids
        assert room.host_id == "P2"
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_last_non_returned_player_leaves_resets_room():
    async def scenario():
        room = _start_ingame_room("last-leave-reset", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        for player_id in ("P1", "P2", "P3"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )
        assert room.status == RoomStatus.IN_GAME

        clients["P4"][0].send({"action": "LEAVE_ROOM", "payload": {}})
        await _wait_for(
            clients["P4"][0], "ACTION_SUCCESS", predicate=_return_success("LEAVE_ROOM")
        )

        assert room.status == RoomStatus.WAITING
        assert room.engine is None
        assert room.returned_to_lobby_player_ids == set()

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_last_non_returned_player_grace_expiry_resets_room(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.4)
        room = _start_ingame_room("grace-last", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        await _disconnect_client(clients, "P4")
        for player_id in ("P1", "P2", "P3"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )
        # P4 remains a current member (offline, within grace) so no reset yet.
        assert room.status == RoomStatus.IN_GAME
        assert "P4" in room.player_ids

        await asyncio.sleep(0.6)
        assert "P4" not in room.player_ids
        assert room.status == RoomStatus.WAITING
        assert room.engine is None

        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. Reconnect and disconnect grace
# ---------------------------------------------------------------------------

def test_non_returned_disconnect_reconnect_restores_result_screen(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 10)
        room = _start_ingame_room("result-reconnect", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        await _disconnect_client(clients, "P3")
        assert "P3" not in room.returned_to_lobby_player_ids
        assert "P3" in room.player_ids

        await _connect_replacement(clients, room, "P3")
        _, game_state = await _wait_for(
            clients["P3"][0],
            "GAME_STATE_UPDATE",
            predicate=lambda m: m["payload"].get("returned_to_lobby_player_ids")
            == ["P2"],
        )
        assert game_state["payload"]["phase"] == "GAME_OVER"
        assert game_state["payload"]["room_status"] == "IN_GAME"
        assert "P3" not in game_state["payload"]["returned_to_lobby_player_ids"]

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_returned_player_disconnect_reconnect_restores_lobby_state(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 10)
        room = _start_ingame_room("lobby-reconnect", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        await _disconnect_client(clients, "P2")
        # Returned status is preserved during the disconnect grace period.
        assert "P2" in room.returned_to_lobby_player_ids

        await _connect_replacement(clients, room, "P2")
        _, game_state = await _wait_for(
            clients["P2"][0],
            "GAME_STATE_UPDATE",
            predicate=lambda m: m["payload"].get("returned_to_lobby_player_ids")
            == ["P2"],
        )
        assert game_state["payload"]["room_status"] == "IN_GAME"
        assert "P2" in game_state["payload"]["returned_to_lobby_player_ids"]

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_returned_player_grace_expiry_removed_cleanly(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.02)
        room = _start_ingame_room("returned-grace", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())
        await _disconnect_client(clients, "P2")
        assert "P2" in room.returned_to_lobby_player_ids

        await asyncio.sleep(0.06)
        assert "P2" not in room.player_ids
        assert "P2" not in room.returned_to_lobby_player_ids
        assert "token-P2" not in routes.session_tokens
        # Others are still viewing results, so the room is not reset.
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_host_disconnect_reconnect_preserves_host_during_terminal(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 10)
        room = _start_ingame_room("host-term-reconnect", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        await _disconnect_client(clients, "P1")
        assert room.host_id == "P1"

        await _connect_replacement(clients, room, "P1")
        assert room.host_id == "P1"
        assert room.status == RoomStatus.IN_GAME

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_host_timeout_during_terminal_transfers_host(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.02)
        room = _start_ingame_room("host-term-timeout", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        await _disconnect_client(clients, "P1")
        await asyncio.sleep(0.06)

        assert "P1" not in room.player_ids
        assert room.host_id == "P2"
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None

        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 7. Authorization
# ---------------------------------------------------------------------------

def test_invalid_session_rejected_in_websocket_action():
    async def scenario():
        room = _start_ingame_room("bad-session", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        routes.session_tokens.clear()

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        _, error = await _wait_for(clients["P2"][0], "ERROR")

        assert error["payload"]["code"] == "INVALID_SESSION"
        assert error["payload"]["action"] == "RETURN_TO_LOBBY"
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None
        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_non_member_rejected():
    async def scenario():
        room = _start_ingame_room("non-member", 4)
        _drive_to_terminal(room)
        with pytest.raises(PlayerNotInRoom):
            room.return_to_lobby("ghost")

    asyncio.run(scenario())


def test_non_member_rejected_with_stable_ws_error_code():
    async def scenario():
        room = _create_room("non-member-ws", 4)
        clients = await _start_clients(room)

        room.remove_player("P1")

        clients["P1"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        _, error = await _wait_for(clients["P1"][0], "ERROR")

        assert error["payload"]["code"] == "PLAYER_NOT_IN_ROOM"
        assert error["payload"]["action"] == "RETURN_TO_LOBBY"
        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_waiting_room_rejected():
    async def scenario():
        room = _create_room("waiting-reject", 4)
        with pytest.raises(InvalidPhase):
            room.return_to_lobby("P1")

    asyncio.run(scenario())


def test_game_setup_rejected():
    async def scenario():
        room = _create_room("setup-reject", 4)
        room.start_game("P1", set(room.player_ids))
        assert room.status == RoomStatus.GAME_SETUP
        with pytest.raises(InvalidPhase):
            room.return_to_lobby("P1")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "phase",
    [
        GamePhase.PLAYING,
        GamePhase.TRICK_RESOLUTION,
        GamePhase.TRUMP_REVEAL_DISPLAY,
        GamePhase.HIDDEN_CARD_RETURN,
        GamePhase.FINAL_SCORE_DISPLAY,
    ],
)
def test_active_game_phases_rejected(phase):
    async def scenario():
        room = _start_ingame_room(f"phase-{phase.value}", 4)
        room.engine.state.phase = phase
        with pytest.raises(InvalidPhase):
            room.return_to_lobby("P1")

    asyncio.run(scenario())


def test_ingame_with_missing_engine_rejected_as_game_not_started():
    async def scenario():
        room = _start_ingame_room("missing-engine", 4)
        room.engine = None
        with pytest.raises(GameNotStarted):
            room.return_to_lobby("P1")
        assert room.status == RoomStatus.IN_GAME

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 8. Races and idempotency
# ---------------------------------------------------------------------------

def test_simultaneous_partial_returns_preserve_both_entries():
    room = _start_ingame_room("race-entries", 4)
    _drive_to_terminal(room)

    barrier = threading.Barrier(2)

    def return_player(player_id):
        barrier.wait()
        return room.return_to_lobby(player_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(return_player, player_id)
            for player_id in ("P3", "P4")
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results == [False, False]
    assert set(room.returned_to_lobby_player_ids) == {"P3", "P4"}
    assert room.status == RoomStatus.IN_GAME
    assert room.engine is not None


def test_last_two_simultaneous_returns_produce_exactly_one_reset():
    room = _start_ingame_room("race-last-two", 4)
    _drive_to_terminal(room)
    room.return_to_lobby("P1")
    room.return_to_lobby("P2")
    generation_before = room.match_generation

    barrier = threading.Barrier(2)

    def return_player(player_id):
        barrier.wait()
        return room.return_to_lobby(player_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(return_player, player_id)
            for player_id in ("P3", "P4")
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert room.status == RoomStatus.WAITING
    assert room.engine is None
    assert room.match_generation == generation_before + 1


def test_return_versus_leave_race_produces_exactly_one_reset():
    room = _start_ingame_room("race-return-leave", 4)
    _drive_to_terminal(room)
    room.return_to_lobby("P1")
    room.return_to_lobby("P2")
    generation_before = room.match_generation

    barrier = threading.Barrier(2)

    def return_p3():
        barrier.wait()
        return room.return_to_lobby("P3")

    def leave_p4():
        barrier.wait()
        room.remove_player("P4")
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(return_p3), executor.submit(leave_p4)]
        for future in futures:
            future.result(timeout=10)

    assert room.status == RoomStatus.WAITING
    assert room.engine is None
    assert room.returned_to_lobby_player_ids == set()
    assert room.match_generation == generation_before + 1


def test_return_versus_disconnect_timeout_race_single_reset(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.02)
        room = _start_ingame_room("race-timeout", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        for player_id in ("P1", "P2"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )

        await _disconnect_client(clients, "P4")
        generation_before = room.match_generation

        # P3 returns while P4's disconnect grace expiry is pending; whichever
        # order they land in, exactly one final reset may occur.
        clients["P3"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await asyncio.sleep(0.06)

        assert room.status == RoomStatus.WAITING
        assert room.engine is None
        assert room.match_generation == generation_before + 1

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_duplicate_return_by_returned_player_is_idempotent():
    async def scenario():
        room = _start_ingame_room("dup-idem", 4)
        _drive_to_terminal(room)
        generation = room.match_generation
        engine = room.engine

        assert room.return_to_lobby("P2") is False
        assert room.return_to_lobby("P2") is False

        assert set(room.returned_to_lobby_player_ids) == {"P2"}
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is engine
        assert room.match_generation == generation

    asyncio.run(scenario())


def test_duplicate_ws_return_is_idempotent_success():
    async def scenario():
        room = _start_ingame_room("dup-ws", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        requester = clients["P2"][0]
        requester.send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(requester, "ACTION_SUCCESS", predicate=_return_success())
        generation = room.match_generation

        requester.send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(requester, "ACTION_SUCCESS", predicate=_return_success())

        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not None
        assert room.match_generation == generation
        assert set(room.returned_to_lobby_player_ids) == {"P2"}

        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_duplicate_return_after_final_reset_is_stable_error():
    async def scenario():
        room = _start_ingame_room("dup-after", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)

        for player_id in ("P1", "P2", "P3"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )

        requester = clients["P4"][0]
        requester.send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(requester, "ACTION_SUCCESS", predicate=_return_success())
        assert room.status == RoomStatus.WAITING

        requester.send({"action": "RETURN_TO_LOBBY", "payload": {}})
        _, error = await _wait_for(requester, "ERROR")
        assert error["payload"]["code"] == "INVALID_PHASE"
        assert error["payload"]["action"] == "RETURN_TO_LOBBY"
        assert room.status == RoomStatus.WAITING
        assert room.engine is None

        await _terminate_clients(clients)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 9. Match task lifecycle
# ---------------------------------------------------------------------------

def _apply_reset_transition(room):
    room.engine = None
    room.match_generation += 1
    room._status = RoomStatus.WAITING


def _trick_resolution_room(room_id, player_count):
    room = _start_ingame_room(room_id, player_count, "normal")
    state = room.engine.state
    state.phase = GamePhase.PLAYING
    state.current_turn = "P1"
    ranks = list(Rank)
    for index, player_id in enumerate(state.seat_order):
        state.hands[player_id] = [
            Card(Suit.HEARTS, ranks[index]),
            Card(Suit.CLUBS, ranks[index]),
        ]
    for _ in range(player_count):
        player_id = state.current_turn
        room.engine.play_card(player_id, state.hands[player_id][0])
    assert state.phase == GamePhase.TRICK_RESOLUTION
    return room


def test_partial_return_keeps_match_task_registries():
    async def scenario():
        room = _start_ingame_room("registry-kept", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)
        room_id = room.room_id

        routes._trick_resolution_tasks[room_id] = FakeTask()
        routes._final_score_display_tasks[room_id] = FakeTask()
        routes._trump_reveal_display_tasks[room_id] = FakeTask()
        routes._hidden_card_return_tasks[room_id] = FakeTask()

        clients["P2"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P2"][0], "ACTION_SUCCESS", predicate=_return_success())

        assert room_id in routes._trick_resolution_tasks
        assert room_id in routes._final_score_display_tasks
        assert room_id in routes._trump_reveal_display_tasks
        assert room_id in routes._hidden_card_return_tasks
        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_final_return_clears_match_task_registries():
    async def scenario():
        room = _start_ingame_room("registry-cleared", 4)
        _drive_to_terminal(room)
        clients = await _start_clients(room)
        room_id = room.room_id

        routes._trick_resolution_tasks[room_id] = FakeTask()
        routes._final_score_display_tasks[room_id] = FakeTask()
        routes._trump_reveal_display_tasks[room_id] = FakeTask()
        routes._hidden_card_return_tasks[room_id] = FakeTask()

        for player_id in ("P1", "P2", "P3"):
            clients[player_id][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
            await _wait_for(
                clients[player_id][0], "ACTION_SUCCESS", predicate=_return_success()
            )
        clients["P4"][0].send({"action": "RETURN_TO_LOBBY", "payload": {}})
        await _wait_for(clients["P4"][0], "ACTION_SUCCESS", predicate=_return_success())

        assert room.status == RoomStatus.WAITING
        assert room_id not in routes._trick_resolution_tasks
        assert room_id not in routes._final_score_display_tasks
        assert room_id not in routes._trump_reveal_display_tasks
        assert room_id not in routes._hidden_card_return_tasks
        await _terminate_clients(clients)

    asyncio.run(scenario())


def test_stale_trick_task_cannot_mutate_waiting(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.02)
        room = _trick_resolution_room("stale-trick", 4)
        state = room.engine.state
        task = routes._schedule_trick_resolution(room.room_id)
        assert task is not None

        _apply_reset_transition(room)
        assert room.status == RoomStatus.WAITING
        await task

        assert room.status == RoomStatus.WAITING
        assert state.phase == GamePhase.TRICK_RESOLUTION
        assert len(state.completed_tricks) == 0

    asyncio.run(scenario())


def test_stale_final_score_task_cannot_mutate_waiting(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.02)
        room = _start_ingame_room("stale-final", 4)
        state = room.engine.state
        state.phase = GamePhase.FINAL_SCORE_DISPLAY
        state.final_result = "TeamB"
        task = routes._schedule_final_score_display(room.room_id)
        assert task is not None

        _apply_reset_transition(room)
        await task

        assert room.status == RoomStatus.WAITING
        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY

    asyncio.run(scenario())


def test_stale_reveal_task_cannot_mutate_waiting(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "TRUMP_REVEAL_DISPLAY_DURATION_SECONDS", 0.02)
        room = _start_ingame_room("stale-reveal", 4, "hidden")
        state = room.engine.state
        state.phase = GamePhase.TRUMP_REVEAL_DISPLAY
        state.reveal_generation = 1
        task = routes._schedule_trump_reveal_display(room.room_id)
        assert task is not None

        _apply_reset_transition(room)
        await task

        assert room.status == RoomStatus.WAITING
        assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY

    asyncio.run(scenario())


def test_stale_hidden_card_return_task_cannot_mutate_waiting(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "HIDDEN_CARD_RETURN_DURATION_SECONDS", 0.02)
        room = _start_ingame_room("stale-return", 4, "hidden")
        state = room.engine.state
        state.phase = GamePhase.HIDDEN_CARD_RETURN
        state.reveal_generation = 1
        task = routes._schedule_hidden_card_return(room.room_id)
        assert task is not None

        _apply_reset_transition(room)
        await task

        assert room.status == RoomStatus.WAITING
        assert state.phase == GamePhase.HIDDEN_CARD_RETURN

    asyncio.run(scenario())


def test_stale_tasks_cannot_mutate_a_new_rematch(monkeypatch):
    async def scenario():
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.02)
        room = _trick_resolution_room("stale-rematch", 4)
        stale_state = room.engine.state
        task = routes._schedule_trick_resolution(room.room_id)

        _apply_reset_transition(room)
        room.start_game("P1", set(room.player_ids))
        room.select_first_player("P1", "P1")
        new_engine = room.engine
        new_version = new_engine.state.version
        new_hands = {
            player_id: list(hand)
            for player_id, hand in new_engine.state.hands.items()
        }

        await task

        assert room.engine is new_engine
        assert new_engine.state.version == new_version
        assert new_engine.state.deal_generation == 1
        assert {
            player_id: list(hand)
            for player_id, hand in new_engine.state.hands.items()
        } == new_hands
        assert stale_state.phase == GamePhase.TRICK_RESOLUTION

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 10. Fresh rematch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_fresh_rematch_clears_returned_set_and_state(player_count, trump_mode):
    async def scenario():
        room = _start_ingame_room(
            f"rematch-{player_count}-{trump_mode}", player_count, trump_mode
        )
        old_engine = room.engine
        _drive_to_terminal(room)
        _return_all(room, room.player_ids)
        assert room.status == RoomStatus.WAITING
        assert room.engine is None
        generation_after_reset = room.match_generation

        room.start_game("P1", set(room.player_ids))
        assert room.match_generation == generation_after_reset + 1
        assert room.returned_to_lobby_player_ids == set()
        if trump_mode == "hidden":
            room.select_trump_hider("P1", "P1")
        room.select_first_player("P1", "P1")
        assert room.status == RoomStatus.IN_GAME
        assert room.engine is not old_engine
        assert room.engine.state.game_id == room.room_id

        state = room.engine.state
        expected = {4: 12, 6: 8, 8: 6}[player_count]
        assert all(len(hand) == expected for hand in state.hands.values())
        all_cards = [card for hand in state.hands.values() for card in hand]
        assert len(all_cards) == 48
        assert len(set(all_cards)) == 48
        assert state.deal_generation == 1
        assert state.phase == (
            GamePhase.HIDDEN_TRUMP_SELECTION
            if trump_mode == "hidden"
            else GamePhase.PLAYING
        )
        assert state.completed_tricks == []
        assert all(not suits for suits in state.captured_mendis.values())
        assert all(team.tricks_won == 0 for team in state.teams.values())
        assert all(team.tens_captured == 0 for team in state.teams.values())
        assert state.final_result is None
        assert state.current_trick.played_cards == []
        assert state.trump_state.suit is None
        assert state.trump_state.status.value == "NONE"

    asyncio.run(scenario())


def test_fresh_rematch_gets_fresh_secure_deal_and_audit():
    async def scenario():
        room = _start_ingame_room("fresh-audit", 4)
        _drive_to_terminal(room)
        _return_all(room, room.player_ids)

        room.start_game("P1", set(room.player_ids))
        room.select_first_player("P1", "P1")
        engine = room.engine

        assert engine.verify_deal_audit() is True
        audit = engine._deal_audit
        assert audit.context["game_id"] == room.room_id
        assert audit.context["room_id"] == room.room_id
        assert audit.context["player_count"] == 4
        assert audit.context["trump_mode"] == "normal"

    asyncio.run(scenario())


def test_no_old_match_state_leaks_into_lobby_snapshot():
    async def scenario():
        room = _start_ingame_room("no-leak", 4)
        _drive_to_terminal(room)
        _return_all(room, room.player_ids)

        state = room.get_state()
        assert set(state.keys()) == {
            "room_id",
            "status",
            "host_id",
            "player_count",
            "trump_mode",
            "team_names",
            "returned_to_lobby_player_ids",
            "players",
        }
        assert state["status"] == "WAITING"
        assert state["returned_to_lobby_player_ids"] == []
        assert room.engine is None

    asyncio.run(scenario())


def test_reset_after_real_timer_terminal_then_individual_returns(monkeypatch):
    async def scenario():
        room = _start_ingame_room("timer-terminal", 4)
        state = room.engine.state
        state.phase = GamePhase.PLAYING
        state.current_turn = "P1"
        state.hands = {
            player.player_id: [
                Card(
                    Suit.HEARTS,
                    Rank.TEN if player.player_id == "P1" else Rank.ACE
                    if player.player_id == "P2"
                    else Rank.THREE,
                )
            ]
            for player in state.players
        }
        for _ in range(state.player_count):
            player_id = state.current_turn
            room.engine.play_card(player_id, state.hands[player_id][0])
        assert state.phase == GamePhase.TRICK_RESOLUTION

        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.001)
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.001)
        await routes._schedule_trick_resolution(room.room_id)
        await routes._final_score_display_tasks[room.room_id]
        assert state.phase in (GamePhase.GAME_OVER, GamePhase.DRAW)

        assert room.return_to_lobby("P2") is False
        assert room.status == RoomStatus.IN_GAME
        assert room.engine.state.phase == state.phase
        assert set(room.returned_to_lobby_player_ids) == {"P2"}

        for player_id in ("P1", "P3"):
            assert room.return_to_lobby(player_id) is False
        assert room.return_to_lobby("P4") is True
        assert room.status == RoomStatus.WAITING
        assert room.engine is None

    asyncio.run(scenario())
