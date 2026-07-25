from typing import Optional
from .room import GameRoom
from .exceptions import RoomAlreadyExists, RoomNotFound

class RoomManager:
    """Manages a collection of GameRooms, ensuring isolation between them."""

    def __init__(self):
        self._rooms: dict[str, GameRoom] = {}

    def create_room(self, room_id: str) -> GameRoom:
        if room_id in self._rooms:
            raise RoomAlreadyExists(f"Room {room_id} already exists.")
        
        room = GameRoom(room_id)
        self._rooms[room_id] = room
        return room

    def get_room(self, room_id: str) -> GameRoom:
        if room_id not in self._rooms:
            raise RoomNotFound(f"Room {room_id} does not exist.")
        return self._rooms[room_id]

    def delete_room(self, room_id: str) -> None:
        if room_id not in self._rooms:
            raise RoomNotFound(f"Room {room_id} does not exist.")
        del self._rooms[room_id]

    def join_room(self, room_id: str, player_id: str, display_name: Optional[str] = None) -> None:
        room = self.get_room(room_id)
        room.add_player(player_id, display_name)

    def leave_room(self, room_id: str, player_id: str) -> None:
        room = self.get_room(room_id)
        room.remove_player(player_id)
