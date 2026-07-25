from typing import Dict, Set
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # room_id -> {player_id -> WebSocket}
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, room_id: str, player_id: str, websocket: WebSocket):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = {}
        
        # Replace the old connection if one exists
        if player_id in self.active_connections[room_id]:
            try:
                await self.active_connections[room_id][player_id].close(code=1008, reason="Replaced by new connection")
            except Exception:
                pass
                
        self.active_connections[room_id][player_id] = websocket

    def disconnect(self, room_id: str, player_id: str):
        if room_id in self.active_connections:
            if player_id in self.active_connections[room_id]:
                del self.active_connections[room_id][player_id]
            if not self.active_connections[room_id]:
                del self.active_connections[room_id]

    def get_connection(self, room_id: str, player_id: str) -> WebSocket | None:
        if room_id in self.active_connections:
            return self.active_connections[room_id].get(player_id)
        return None

    def get_connected_player_ids(self, room_id: str) -> Set[str]:
        if room_id in self.active_connections:
            return set(self.active_connections[room_id].keys())
        return set()

    async def broadcast(self, room_id: str, message: dict):
        if room_id in self.active_connections:
            for connection in self.active_connections[room_id].values():
                try:
                    await connection.send_json(message)
                except Exception:
                    # Ignore failing connections during broadcast
                    pass

    async def send_to_player(self, room_id: str, player_id: str, message: dict):
        connection = self.get_connection(room_id, player_id)
        if connection:
            try:
                await connection.send_json(message)
            except Exception:
                pass
