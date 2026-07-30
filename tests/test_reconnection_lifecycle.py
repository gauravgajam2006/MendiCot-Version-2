import asyncio

import pytest

from mendicot.api import routes
from mendicot.api.connection_manager import ConnectionManager
from mendicot.exceptions import RoomFull, RoomNotFound


class Socket:
    def __init__(self):
        self.accepted = False
        self.closed = False
        self.messages = []

    async def accept(self):
        self.accepted = True

    async def close(self, **kwargs):
        self.closed = True

    async def send_json(self, message):
        self.messages.append(message)


def clear_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()


@pytest.fixture(autouse=True)
def reset_state():
    clear_state()
    yield
    clear_state()


def seed_room(player_ids=("P1",), capacity=4):
    room = routes.room_manager.create_room("ROOM", capacity)
    for player_id in player_ids:
        room.add_player(player_id, f"Name {player_id}")
        routes.session_tokens[f"token-{player_id}"] = {
            "room_id": "room",
            "player_id": player_id,
        }
    return room


async def reconnect(room_id, player_id, socket):
    await routes.connection_manager.connect(room_id, player_id, socket)
    routes._cancel_disconnect_cleanup(room_id, player_id)


def test_reconnect_recreates_missing_room_connection_map():
    async def run():
        manager = ConnectionManager()
        socket = Socket()
        await manager.connect("ROOM", "P1", socket)
        manager.active_connections.clear()
        replacement = Socket()
        await manager.connect("room", "P1", replacement)
        assert manager.get_connection("ROOM", "P1") is replacement
    asyncio.run(run())


def test_refresh_reconnect_preserves_identity_and_marks_player_online(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.04)
        room = seed_room(("P1", "P2"))
        first = Socket()
        await reconnect("room", "P1", first)
        await routes._handle_socket_disconnect("room", "P1", first)
        offline = routes._get_room_state("room")
        assert offline["host_id"] == "P1"
        assert offline["players"][0]["is_online"] is False
        assert room.player_count == 2

        replacement = Socket()
        await reconnect("ROOM", "P1", replacement)
        online = routes._get_room_state("room")
        player = online["players"][0]
        assert player == {
            "player_id": "P1",
            "display_name": "Name P1",
            "is_online": True,
        }
        assert online["host_id"] == "P1"
        await asyncio.sleep(0.06)
        assert room.player_ids == ["P1", "P2"]
        assert routes.session_tokens["token-P1"]["player_id"] == "P1"
    asyncio.run(run())


def test_stale_socket_cleanup_and_repeated_reconnects_are_stable(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.04)
        room = seed_room()
        socket_a, socket_b, socket_c = Socket(), Socket(), Socket()
        await reconnect("room", "P1", socket_a)
        await routes._handle_socket_disconnect("room", "P1", socket_a)
        await reconnect("room", "P1", socket_b)
        await routes._handle_socket_disconnect("room", "P1", socket_a)
        assert routes.connection_manager.get_connection("room", "P1") is socket_b

        await reconnect("room", "P1", socket_c)
        await routes._handle_socket_disconnect("room", "P1", socket_b)
        assert routes.connection_manager.get_connection("room", "P1") is socket_c
        assert room.player_ids == ["P1"]
        await asyncio.sleep(0.06)
        assert room.player_ids == ["P1"]
    asyncio.run(run())


def test_full_room_keeps_offline_capacity_then_frees_slot_after_timeout(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)
        room = seed_room(("P1", "P2", "P3", "P4"), capacity=4)
        socket = Socket()
        await reconnect("room", "P1", socket)
        await routes._handle_socket_disconnect("room", "P1", socket)
        with pytest.raises(RoomFull):
            room.add_player("P5")

        replacement = Socket()
        await reconnect("room", "P1", replacement)
        assert room.player_count == 4
        with pytest.raises(RoomFull):
            room.add_player("P5")

        await routes._handle_socket_disconnect("room", "P1", replacement)
        await asyncio.sleep(0.03)
        assert "P1" not in room.player_ids
        assert "token-P1" not in routes.session_tokens
        room.add_player("P5")
        assert room.player_count == 4
    asyncio.run(run())


def test_host_remains_host_during_grace_and_reconnect(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.04)
        room = seed_room(("host", "next"))
        host_socket = Socket()
        await reconnect("room", "host", host_socket)
        await routes._handle_socket_disconnect("room", "host", host_socket)
        assert room.host_id == "host"
        assert routes._get_room_state("room")["host_id"] == "host"

        replacement = Socket()
        await reconnect("room", "host", replacement)
        await asyncio.sleep(0.06)
        assert room.host_id == "host"
        assert routes._get_room_state("room")["players"][0]["is_online"] is True
    asyncio.run(run())


def test_host_timeout_chooses_lowest_seat_online_player(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)
        room = seed_room(("host", "offline-next", "online-low", "online-high"))
        await reconnect("room", "online-high", Socket())
        await reconnect("room", "online-low", Socket())
        host_socket = Socket()
        await reconnect("room", "host", host_socket)
        await routes._handle_socket_disconnect("room", "host", host_socket)
        assert room.host_id == "host"
        await asyncio.sleep(0.03)
        assert room.player_ids == ["offline-next", "online-low", "online-high"]
        assert room.host_id == "online-low"
        assert routes._get_room_state("room")["host_id"] == "online-low"
    asyncio.run(run())


def test_intentional_host_leave_transfers_immediately():
    async def run():
        room = seed_room(("host", "next"))
        await reconnect("room", "next", Socket())
        host_socket = Socket()
        await reconnect("room", "host", host_socket)
        routes.connection_manager.disconnect("room", "host", host_socket)
        routes._cancel_disconnect_cleanup("room", "host")
        await routes._remove_lobby_player("room", "host")
        assert room.host_id == "next"
        assert room.player_ids == ["next"]
        assert "token-host" not in routes.session_tokens
    asyncio.run(run())


def test_final_player_timeout_deletes_room_and_all_state(monkeypatch):
    async def run():
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0.01)
        seed_room()
        socket = Socket()
        await reconnect("room", "P1", socket)
        await routes._handle_socket_disconnect("room", "P1", socket)
        await asyncio.sleep(0.03)
        with pytest.raises(RoomNotFound):
            routes.room_manager.get_room("room")
        assert "room" not in routes.connection_manager.active_connections
        assert not routes.session_tokens
        assert not routes._disconnect_cleanup_tasks
    asyncio.run(run())