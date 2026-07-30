from typing import Dict, Set
from fastapi import WebSocket
from mendicot.room_ids import normalize_room_id

class ConnectionManager:
    def __init__(self):
        # room_id -> {player_id -> WebSocket}
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, room_id: str, player_id: str, websocket: WebSocket):
        room_id = normalize_room_id(room_id)
        await websocket.accept()
        # Install the replacement before closing the old socket. Closing can run
        # the old endpoint's disconnect handler immediately; identity-aware
        # cleanup then leaves this newer socket untouched.
        room_connections = self.active_connections.setdefault(room_id, {})
        old_connection = room_connections.get(player_id)
        room_connections[player_id] = websocket
        if old_connection is not None and old_connection is not websocket:
            try:
                await old_connection.close(code=1008, reason="Replaced by new connection")
            except Exception:
                pass

    def disconnect(
        self, room_id: str, player_id: str, websocket: WebSocket
    ) -> bool:
        """Remove a player's active connection.

        Only remove ``websocket`` when it is still the exact registered
        connection. This prevents an old, replaced socket from
        disconnecting a newer reconnect.
        """
        room_id = normalize_room_id(room_id)
        room_connections = self.active_connections.get(room_id)
        if room_connections is not None:
            connection = room_connections.get(player_id)
            if connection is websocket:
                del room_connections[player_id]
                if not room_connections and self.active_connections.get(room_id) is room_connections:
                    self.active_connections.pop(room_id, None)
                return True
        return False

    def clear_room(self, room_id: str) -> None:
        """Remove all active connection state for a deleted room."""
        self.active_connections.pop(normalize_room_id(room_id), None)

    def get_connection(self, room_id: str, player_id: str) -> WebSocket | None:
        room_id = normalize_room_id(room_id)
        if room_id in self.active_connections:
            return self.active_connections[room_id].get(player_id)
        return None

    def get_connected_player_ids(self, room_id: str) -> Set[str]:
        room_id = normalize_room_id(room_id)
        if room_id in self.active_connections:
            return set(self.active_connections[room_id].keys())
        return set()

    async def broadcast(self, room_id: str, message: dict):
        room_id = normalize_room_id(room_id)
        if room_id in self.active_connections:
            for connection in list(self.active_connections[room_id].values()):
                try:
                    await connection.send_json(message)
                except Exception:
                    # Ignore failing connections during broadcast
                    pass

    async def send_to_player(self, room_id: str, player_id: str, message: dict):
        room_id = normalize_room_id(room_id)
        connection = self.get_connection(room_id, player_id)
        if connection:
            try:
                await connection.send_json(message)
            except Exception:
                pass
