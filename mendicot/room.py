import enum
from dataclasses import dataclass
from typing import Optional

from .engine import MendiCotEngine
from .enums import TeamId
from .room_ids import normalize_room_id
from .models import Player
from .exceptions import (
    DuplicatePlayer,
    RoomFull,
    GameAlreadyStarted,
    PlayerNotInRoom,
    InvalidRoomSize,
    NotRoomHost,
    InvalidTeam,
    PlayerOffline,
    RoomNotFull,
    TeamsUnbalanced,
)


class RoomStatus(str, enum.Enum):
    WAITING = "WAITING"
    IN_GAME = "IN_GAME"


@dataclass
class RoomPlayer:
    player_id: str
    display_name: str
    team_id: TeamId
    seat_index: int
    is_online: bool = False


class GameRoom:
    """Manages player sessions and room lifecycle, acting as a wrapper around the engine."""

    MAX_PLAYERS = 8
    VALID_START_COUNTS = (4, 6, 8)

    def __init__(
        self,
        room_id: str,
        configured_player_count: int = MAX_PLAYERS,
        trump_mode: str = "normal",
    ):
        self.room_id = normalize_room_id(room_id)
        self.configured_player_count = configured_player_count
        self.trump_mode = trump_mode
        self.host_id: Optional[str] = None
        self._players: list[RoomPlayer] = []
        self._next_join_index = 0
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

    def _next_available_seat_index(self) -> int:
        occupied_seats = {player.seat_index for player in self._players}
        return next(
            seat_index
            for seat_index in range(self.configured_player_count)
            if seat_index not in occupied_seats
        )

    def get_player(self, player_id: str) -> RoomPlayer:
        player = next(
            (candidate for candidate in self._players if candidate.player_id == player_id),
            None,
        )
        if player is None:
            raise PlayerNotInRoom(f"Player {player_id} not found in the room.")
        return player

    def add_player(self, player_id: str, display_name: Optional[str] = None) -> None:
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("Cannot join room after the game has started.")
        
        if player_id in self.player_ids:
            raise DuplicatePlayer(f"Player {player_id} is already in the room.")
        
        if self.player_count >= self.configured_player_count:
            raise RoomFull(
                f"Room has reached configured capacity ({self.configured_player_count})."
            )

        seat_index = self._next_available_seat_index()
        player = RoomPlayer(
            player_id=player_id,
            display_name=display_name or player_id,
            team_id=(
                TeamId.TEAM_A
                if self._next_join_index % 2 == 0
                else TeamId.TEAM_B
            ),
            seat_index=seat_index,
        )
        self._players.append(player)
        self._next_join_index += 1

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

    def set_player_online(self, player_id: str, is_online: bool) -> None:
        self.get_player(player_id).is_online = is_online

    def switch_team(self, player_id: str, team_id: str | None) -> None:
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("Cannot switch teams after the game has started.")

        try:
            requested_team = TeamId(team_id)
        except (TypeError, ValueError) as exc:
            raise InvalidTeam(
                f"Team must be one of: {TeamId.TEAM_A.value}, {TeamId.TEAM_B.value}."
            ) from exc

        # Assigning the existing value is intentionally idempotent.
        self.get_player(player_id).team_id = requested_team

    def _validate_teams_for_start(self) -> None:
        team_counts = {team_id: 0 for team_id in TeamId}
        for player in self._players:
            try:
                team_counts[TeamId(player.team_id)] += 1
            except (TypeError, ValueError) as exc:
                raise TeamsUnbalanced(
                    "Every player must belong to TeamA or TeamB."
                ) from exc

        required_team_size = self.configured_player_count // 2
        if any(count != required_team_size for count in team_counts.values()):
            raise TeamsUnbalanced(
                f"Teams must contain {required_team_size} players each."
            )

    def _build_engine_players(self) -> list[Player]:
        """Interleave selected teams while preserving lobby order within each team."""
        lobby_order = sorted(self._players, key=lambda player: player.seat_index)
        first_team = TeamId(lobby_order[0].team_id)
        second_team = (
            TeamId.TEAM_B if first_team == TeamId.TEAM_A else TeamId.TEAM_A
        )
        players_by_team = {
            team_id: [
                player
                for player in lobby_order
                if TeamId(player.team_id) == team_id
            ]
            for team_id in TeamId
        }

        engine_order: list[RoomPlayer] = []
        for first_player, second_player in zip(
            players_by_team[first_team], players_by_team[second_team]
        ):
            engine_order.extend((first_player, second_player))

        return [
            Player(
                player_id=room_player.player_id,
                team_id=TeamId(room_player.team_id).value,
                seat_index=engine_seat_index,
            )
            for engine_seat_index, room_player in enumerate(engine_order)
        ]

    def start_game(
        self,
        requester_id: str,
        online_player_ids: set[str] | None = None,
    ) -> None:
        if requester_id != self.host_id:
            raise NotRoomHost("Only the room host can start the game.")
        
        if self._status == RoomStatus.IN_GAME:
            raise GameAlreadyStarted("The game has already started.")
            
        if self.configured_player_count not in self.VALID_START_COUNTS:
            raise InvalidRoomSize(
                "Configured room size must be 4, 6, or 8 players."
            )

        if self.player_count != self.configured_player_count:
            raise RoomNotFull(
                f"Room requires {self.configured_player_count} players before starting."
            )

        if online_player_ids is None:
            online_player_ids = {
                player.player_id for player in self._players if player.is_online
            }
        offline_players = [
            player.player_id
            for player in sorted(self._players, key=lambda player: player.seat_index)
            if player.player_id not in online_player_ids
        ]
        if offline_players:
            raise PlayerOffline(
                f"All players must be online before starting: {', '.join(offline_players)}."
            )

        self._validate_teams_for_start()

        engine = MendiCotEngine()
        engine.create_game(
            game_id=self.room_id,
            players=self._build_engine_players(),
            host_id=self.host_id,
            hidden_trump_mode=self.trump_mode == "hidden"
        )
        self.engine = engine
        self._status = RoomStatus.IN_GAME

    def get_state(self) -> dict:
        return {
            "room_id": self.room_id,
            "status": self._status.value,
            "host_id": self.host_id,
            "player_count": self.configured_player_count,
            "trump_mode": self.trump_mode,
            "players": [
                {
                    "player_id": p.player_id,
                    "display_name": p.display_name,
                    "team_id": TeamId(p.team_id).value,
                    "seat_index": p.seat_index,
                    "is_online": p.is_online,
                } for p in sorted(
                    self._players, key=lambda player: player.seat_index
                )
            ]
        }
