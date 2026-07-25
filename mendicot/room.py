import enum
from dataclasses import dataclass
from typing import Optional

from .engine import MendiCotEngine
from .models import Player
from .exceptions import (
    DuplicatePlayer,
    RoomFull,
    GameAlreadyStarted,
    PlayerNotInRoom,
    InvalidRoomSize,
    NotRoomHost,
)


class RoomStatus(str, enum.Enum):
    WAITING = "WAITING"
    IN_GAME = "IN_GAME"


@dataclass
class RoomPlayer:
    player_id: str
    display_name: str


class GameRoom:
    """Manages player sessions and room lifecycle, acting as a wrapper around the engine."""

    MAX_PLAYERS = 8
    VALID_START_COUNTS = (4, 6, 8)

    def __init__(self, room_id: str):
        self.room_id = room_id
        self.host_id: Optional[str] = None
        self._players: list[RoomPlayer] = []
        self._status = RoomStatus.WAITING
        self.engine: Optional[MendiCotEngine] = None

    @property
    def player_ids(self) -> list[str]:
        return [p.player_id for p in self._players]

    @property
    def player_count(self) -> int:
        return len(self._players)

    @property
    def status(self) -> RoomStatus:
        return self._status

    def add_player(self, player_id: str, display_name: Optional[str] = None) -> None:
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("Cannot join room after the game has started.")
        
        if player_id in self.player_ids:
            raise DuplicatePlayer(f"Player {player_id} is already in the room.")
        
        if self.player_count >= self.MAX_PLAYERS:
            raise RoomFull(f"Room has reached maximum capacity ({self.MAX_PLAYERS}).")

        player = RoomPlayer(
            player_id=player_id, 
            display_name=display_name or player_id
        )
        self._players.append(player)

        if self.host_id is None:
            self.host_id = player_id

    def remove_player(self, player_id: str) -> None:
        if player_id not in self.player_ids:
            raise PlayerNotInRoom(f"Player {player_id} not found in the room.")
        
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("Cannot remove player after the game has started to preserve game state.")

        self._players = [p for p in self._players if p.player_id != player_id]

        # Transfer host if the host left
        if self.host_id == player_id:
            if self._players:
                self.host_id = self._players[0].player_id
            else:
                self.host_id = None

    def start_game(self, requester_id: str, hidden_trump_mode: bool = False) -> None:
        if requester_id != self.host_id:
            raise NotRoomHost("Only the room host can start the game.")
        
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("The game has already started.")
            
        if self.player_count not in self.VALID_START_COUNTS:
            raise InvalidRoomSize(f"Cannot start game with {self.player_count} players. Must be {', '.join(map(str, self.VALID_START_COUNTS))}.")

        # Convert RoomPlayers to Engine Players
        engine_players = []
        for index, rp in enumerate(self._players):
            # Alternating teams: even index -> TeamA, odd index -> TeamB
            team_id = "TeamA" if index % 2 == 0 else "TeamB"
            engine_players.append(Player(
                player_id=rp.player_id,
                team_id=team_id,
                seat_index=index
            ))

        self.engine = MendiCotEngine()
        self.engine.create_game(
            game_id=self.room_id,
            players=engine_players,
            host_id=self.host_id,
            hidden_trump_mode=hidden_trump_mode
        )
        
        self._status = RoomStatus.IN_GAME

    def get_state(self) -> dict:
        return {
            "room_id": self.room_id,
            "status": self._status.value,
            "host_id": self.host_id,
            "players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name
                } for p in self._players
            ]
        }
