import asyncio
import hashlib
import hmac
import os
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from mendicot.room_manager import RoomManager
from mendicot.room import RoomStatus
from mendicot.enums import GamePhase, Suit
from mendicot.models import Card
from mendicot.exceptions import (
    GameAlreadyStarted,
    GameNotStarted,
    DealAlreadyCompleted,
    DealInvariantFailed,
    InvalidDeckDefinition,
    InvalidPhase,
    InvalidPlayerCount,
    InvalidSeatArrangement,
    InvalidSeatConfiguration,
    InvalidSession,
    InvalidTeam,
    InvalidTeamConfiguration,
    InvalidTeamName,
    MendiCotError,
    NotRoomHost,
    PlayerNotInRoom,
    PlayerOffline,
    RoomNotFound,
    RoomNotFull,
    SetupNotActive,
    TeamsUnbalanced,
    TeamNameTooLong,
    ShuffleCommitmentFailed,
    ShuffleVerificationFailed,
    InvalidTrumpMode,
    InvalidCardIndex,
    NotTrumpHider,
)
from mendicot.room_ids import normalize_room_id
from mendicot import firebase_push
from mendicot.firebase_push import PushSendResult
from mendicot.push_registrations import PushRegistrationStore

from .connection_manager import ConnectionManager
from .schemas import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomRequest,
    JoinRoomResponse,
    ValidateSessionRequest,
    ValidateSessionResponse,
)

@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    firebase_push.initialize_firebase()
    yield


app = FastAPI(title="MendiCot API", lifespan=_app_lifespan)

def _get_allowed_origins() -> list[str]:
    origins_env = os.getenv("ALLOWED_ORIGINS")
    if origins_env:
        origins = [origin.strip() for origin in origins_env.split(",") if origin.strip()]
        # Deduplicate
        seen = set()
        origins = [x for x in origins if not (x in seen or seen.add(x))]
        if origins:
            return origins
    return ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/health/push")
def push_health_check():
    diagnostic = firebase_push.get_firebase_diagnostic()
    return {
        "status": diagnostic.state.value,
        "credential_source": diagnostic.credential_source,
    }


# Global State
room_manager = RoomManager()
connection_manager = ConnectionManager()
push_registrations = PushRegistrationStore()

# session_token -> {"room_id": str, "player_id": str}
session_tokens: Dict[str, Dict[str, str]] = {}
# SHA-256(session_token) -> {"room_id": str, "player_id": str}. Keeping a
# one-way tombstone lets REST preflight distinguish an expired saved session
# from an arbitrary credential without retaining or logging the raw token.
invalidated_session_tokens: Dict[str, Dict[str, str]] = {}
# Offline players retain their lobby seat and credential during this interval.
# Tests may monkeypatch this value to avoid waiting for the production default.
DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS = float(
    os.getenv("MENDICOT_DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", "10")
)
TRICK_DISPLAY_DURATION_SECONDS = float(
    os.getenv("MENDICOT_TRICK_DISPLAY_DURATION_MS", "1800")
) / 1000
FINAL_SCORE_DISPLAY_DURATION_SECONDS = float(
    os.getenv("MENDICOT_FINAL_SCORE_DISPLAY_DURATION_MS", "2500")
) / 1000
TRUMP_REVEAL_DISPLAY_DURATION_SECONDS = float(
    os.getenv("MENDICOT_TRUMP_REVEAL_DURATION_MS", "1500")
) / 1000
HIDDEN_CARD_RETURN_DURATION_SECONDS = float(
    os.getenv("MENDICOT_HIDDEN_CARD_RETURN_DURATION_MS", "800")
) / 1000
_disconnect_cleanup_tasks: Dict[tuple[str, str], asyncio.Task] = {}
_trick_resolution_tasks: Dict[str, asyncio.Task] = {}
_final_score_display_tasks: Dict[str, asyncio.Task] = {}
_trump_reveal_display_tasks: Dict[str, asyncio.Task] = {}
_hidden_card_return_tasks: Dict[str, asyncio.Task] = {}


def _cancel_trick_resolution(room_id: str) -> None:
    """Cancel and forget a room's pending delayed trick resolution."""
    room_id = normalize_room_id(room_id)
    task = _trick_resolution_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()


def _cancel_final_score_display(room_id: str) -> None:
    """Cancel and forget a room's pending final-score transition."""
    room_id = normalize_room_id(room_id)
    task = _final_score_display_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()


def _cancel_trump_reveal_display(room_id: str) -> None:
    """Cancel and forget a room's pending trump reveal display transition."""
    room_id = normalize_room_id(room_id)
    task = _trump_reveal_display_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()


def _cancel_hidden_card_return(room_id: str) -> None:
    """Cancel and forget a room's pending hidden card return transition."""
    room_id = normalize_room_id(room_id)
    task = _hidden_card_return_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()


def _cancel_room_lifecycle(room_id: str, _room=None) -> None:
    _cancel_trick_resolution(room_id)
    _cancel_final_score_display(room_id)
    _cancel_trump_reveal_display(room_id)
    _cancel_hidden_card_return(room_id)


def _cleanup_deleted_room(room_id: str, room=None) -> None:
    _cancel_room_lifecycle(room_id, room)
    push_registrations.remove_room(room_id)


room_manager.on_room_deleted = _cleanup_deleted_room

_ACTION_ERROR_CODES = {
    InvalidDeckDefinition: "INVALID_DECK_DEFINITION",
    InvalidPlayerCount: "INVALID_PLAYER_COUNT",
    InvalidSeatArrangement: "INVALID_SEAT_CONFIGURATION",
    InvalidSeatConfiguration: "INVALID_SEAT_CONFIGURATION",
    InvalidTeamConfiguration: "INVALID_TEAM_CONFIGURATION",
    DealAlreadyCompleted: "DEAL_ALREADY_COMPLETED",
    DealInvariantFailed: "DEAL_INVARIANT_FAILED",
    ShuffleCommitmentFailed: "SHUFFLE_COMMITMENT_FAILED",
    ShuffleVerificationFailed: "SHUFFLE_VERIFICATION_FAILED",
    InvalidTeam: "INVALID_TEAM",
    InvalidTeamName: "INVALID_TEAM_NAME",
    TeamNameTooLong: "TEAM_NAME_TOO_LONG",
    GameAlreadyStarted: "GAME_ALREADY_STARTED",
    GameNotStarted: "GAME_NOT_STARTED",
    PlayerNotInRoom: "PLAYER_NOT_FOUND",
    InvalidPhase: "INVALID_PHASE",
    NotRoomHost: "HOST_ONLY",
    RoomNotFull: "ROOM_NOT_FULL",
    PlayerOffline: "PLAYER_OFFLINE",
    TeamsUnbalanced: "TEAMS_UNBALANCED",
    SetupNotActive: "SETUP_NOT_ACTIVE",
    InvalidTrumpMode: "INVALID_TRUMP_MODE",
    InvalidCardIndex: "INVALID_CARD_INDEX",
    NotTrumpHider: "NOT_TRUMP_HIDER",
    InvalidSession: "INVALID_SESSION",
}


def _action_error_code(error: Exception) -> str:
    return next(
        (
            code
            for exception_type, code in _ACTION_ERROR_CODES.items()
            if isinstance(error, exception_type)
        ),
        error.__class__.__name__,
    )


async def _send_action_error(
    room_id: str,
    player_id: str,
    action: Any,
    code: str,
    message: str,
) -> None:
    delivered = await connection_manager.send_to_player(
        room_id,
        player_id,
        {
            "type": "ERROR",
            "payload": {
                "action": action,
                "code": code,
                "message": message,
            },
        },
    )
    if not delivered:
        await _handle_failed_direct_send(room_id, player_id)


async def _handle_failed_direct_send(room_id: str, player_id: str) -> None:
    """Give a failed one-player reply the normal offline grace treatment."""
    if connection_manager.get_connection(room_id, player_id) is not None:
        return
    try:
        room = room_manager.get_room(room_id)
        room.set_player_online(player_id, False)
    except MendiCotError:
        return
    _schedule_disconnect_cleanup(room_id, player_id)
    await _broadcast_room_state(room_id)
    if (
        room.status in (RoomStatus.GAME_SETUP, RoomStatus.IN_GAME)
        and connection_manager.get_connection(room_id, player_id) is None
    ):
        await connection_manager.broadcast(
            room_id,
            {"type": "PLAYER_OFFLINE", "payload": {"player_id": player_id}},
        )


def _session_token_matches_room(token: str, room_id: str, player_id: str) -> bool:
    session = session_tokens.get(token)
    return (
        session is not None
        and hmac.compare_digest(session["room_id"], normalize_room_id(room_id))
        and hmac.compare_digest(session["player_id"], player_id)
    )


def _session_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _remember_invalidated_session(token: str, session: Dict[str, str]) -> None:
    invalidated_session_tokens[_session_token_digest(token)] = {
        "room_id": normalize_room_id(session["room_id"]),
        "player_id": session["player_id"],
    }


def invalidate_player_tokens(room_id: str, player_id: str) -> None:
    """Invalidate every session token for one player in one room."""
    room_id = normalize_room_id(room_id)
    for token, session in list(session_tokens.items()):
        if session["room_id"] == room_id and session["player_id"] == player_id:
            _remember_invalidated_session(token, session)
            del session_tokens[token]


def invalidate_room_tokens(room_id: str) -> None:
    """Invalidate every session token belonging to a room."""
    room_id = normalize_room_id(room_id)
    for token, session in list(session_tokens.items()):
        if session["room_id"] == room_id:
            _remember_invalidated_session(token, session)
            del session_tokens[token]


def _cancel_disconnect_cleanup(room_id: str, player_id: str) -> None:
    """Cancel a pending offline-player cleanup, if any."""
    key = (normalize_room_id(room_id), player_id)
    task = _disconnect_cleanup_tasks.pop(key, None)
    if task is not None and not task.done():
        task.cancel()


def _cancel_room_disconnect_cleanups(room_id: str) -> None:
    room_id = normalize_room_id(room_id)
    for key in [key for key in _disconnect_cleanup_tasks if key[0] == room_id]:
        _cancel_disconnect_cleanup(*key)


def _next_host_id(room, excluded_player_id: str) -> str | None:
    """Prefer the lowest-seat online player, then the lowest-seat remaining player."""
    candidates = [
        player.player_id
        for player in sorted(room._players, key=lambda player: player.seat_index)
        if player.player_id != excluded_player_id
    ]
    online = connection_manager.get_connected_player_ids(room.room_id)
    return next((player_id for player_id in candidates if player_id in online), None) or (
        candidates[0] if candidates else None
    )


async def _remove_lobby_player(room_id: str, player_id: str) -> bool:
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except MendiCotError:
        return False
    terminal_in_game = room.status == RoomStatus.IN_GAME and room.is_terminal()
    if (
        room.status not in (RoomStatus.WAITING, RoomStatus.GAME_SETUP)
        and not terminal_in_game
    ) or player_id not in room.player_ids:
        return False
    removing_host = room.host_id == player_id
    next_host_id = _next_host_id(room, player_id) if removing_host else None
    room_manager.leave_room(room_id, player_id)
    if removing_host:
        room.host_id = next_host_id
        if room.engine is not None:
            room.engine.state.host_id = next_host_id
    if room.status == RoomStatus.WAITING:
        # The removal completed the final all-returned transition; cancel only
        # match lifecycle tasks. Disconnect-grace cleanup tasks are preserved.
        _cancel_room_lifecycle(room_id)
    invalidate_player_tokens(room_id, player_id)
    push_registrations.remove_player(room_id, player_id)
    if room.player_count == 0:
        _cancel_room_disconnect_cleanups(room_id)
        invalidate_room_tokens(room_id)
        connection_manager.clear_room(room_id)
        room_manager.delete_room(room_id)
        return True
    await _broadcast_room_state(room_id)
    if room.status == RoomStatus.GAME_SETUP:
        await _broadcast_game_states(room_id)
    return True


def _schedule_disconnect_cleanup(room_id: str, player_id: str) -> None:
    """Schedule one task-identity-guarded removal for an offline lobby player."""
    room_id = normalize_room_id(room_id)
    key = (room_id, player_id)
    _cancel_disconnect_cleanup(room_id, player_id)

    async def expire() -> None:
        try:
            await asyncio.sleep(DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS)
            if _disconnect_cleanup_tasks.get(key) is not asyncio.current_task():
                return
            if connection_manager.get_connection(room_id, player_id) is not None:
                return
            await _remove_lobby_player(room_id, player_id)
        except asyncio.CancelledError:
            return
        finally:
            if _disconnect_cleanup_tasks.get(key) is asyncio.current_task():
                _disconnect_cleanup_tasks.pop(key, None)

    _disconnect_cleanup_tasks[key] = asyncio.create_task(expire())


async def _handle_socket_disconnect(
    room_id: str, player_id: str, websocket: WebSocket
) -> None:
    """Mark only the currently registered socket offline and start its grace period."""
    if not connection_manager.disconnect(room_id, player_id, websocket):
        return
    try:
        room = room_manager.get_room(room_id)
        room.set_player_online(player_id, False)
        _schedule_disconnect_cleanup(room_id, player_id)
        await _broadcast_room_state(room_id)
        if room.status in (RoomStatus.GAME_SETUP, RoomStatus.IN_GAME):
            await connection_manager.broadcast(
                room_id,
                {"type": "PLAYER_OFFLINE", "payload": {"player_id": player_id}},
            )
    except MendiCotError:
        pass


@app.post("/api/rooms", response_model=CreateRoomResponse)
async def create_room(request: CreateRoomRequest):
    room_id = normalize_room_id(str(uuid.uuid4())[:8])
    room_manager.create_room(room_id, request.player_count, request.trump_mode)
    return CreateRoomResponse(room_id=room_id)

@app.post("/api/rooms/{room_id}/join", response_model=JoinRoomResponse)
async def join_room(room_id: str, request: JoinRoomRequest):
    room_id = normalize_room_id(room_id)
    try:
        room_manager.join_room(room_id, request.player_id, request.display_name)
    except MendiCotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    session_token = str(uuid.uuid4())
    invalidated_session_tokens.pop(_session_token_digest(session_token), None)
    session_tokens[session_token] = {
        "room_id": room_id,
        "player_id": request.player_id
    }
    
    return JoinRoomResponse(
        room_id=room_id,
        player_id=request.player_id,
        session_token=session_token
    )


def _raise_session_error(status_code: int, code: str, message: str) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


@app.post(
    "/api/rooms/{room_id}/sessions/validate",
    response_model=ValidateSessionResponse,
)
async def validate_session(room_id: str, request: ValidateSessionRequest):
    """Read-only preflight for a saved room session.

    WebSocket authentication remains independent and authoritative when the
    client subsequently opens its socket.
    """
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFound:
        _raise_session_error(404, "ROOM_NOT_FOUND", "Room no longer exists.")

    session = session_tokens.get(request.session_token)
    if session is None:
        invalidated = invalidated_session_tokens.get(
            _session_token_digest(request.session_token)
        )
        if (
            invalidated is not None
            and hmac.compare_digest(invalidated["room_id"], room_id)
            and hmac.compare_digest(invalidated["player_id"], request.player_id)
        ):
            _raise_session_error(
                410, "SESSION_EXPIRED", "Player session has expired."
            )
        _raise_session_error(401, "INVALID_SESSION", "Session token is invalid.")

    if not (
        hmac.compare_digest(session["room_id"], room_id)
        and hmac.compare_digest(session["player_id"], request.player_id)
    ):
        _raise_session_error(401, "INVALID_SESSION", "Session token is invalid.")

    player = next(
        (candidate for candidate in room._players if candidate.player_id == request.player_id),
        None,
    )
    if player is None:
        _raise_session_error(410, "SESSION_EXPIRED", "Player session has expired.")

    return ValidateSessionResponse(
        valid=True,
        room_id=room.room_id,
        player_id=player.player_id,
        display_name=player.display_name,
        room_status=room.status.value,
        player_online=(
            connection_manager.get_connection(room_id, player.player_id) is not None
        ),
    )


def _schedule_failed_disconnect_cleanups(
    room_id: str, failed_connections: list[tuple[str, WebSocket]]
) -> None:
    """Give broadcast failures the same grace-period treatment as disconnects."""
    for player_id, _ in failed_connections:
        if connection_manager.get_connection(room_id, player_id) is None:
            try:
                room_manager.get_room(room_id).set_player_online(player_id, False)
            except MendiCotError:
                pass
            _schedule_disconnect_cleanup(room_id, player_id)

def _get_room_state(room_id: str) -> dict:
    room_id = normalize_room_id(room_id)
    room = room_manager.get_room(room_id)
    connected_players = connection_manager.get_connected_player_ids(room_id)
    for player in room._players:
        player.is_online = player.player_id in connected_players
    return room.get_state()


def _enrich_game_view(room, view: dict) -> dict:
    """Attach authoritative room metadata shared by every game-state snapshot."""
    room_state = room.get_state()
    view["room_id"] = room.room_id
    view["room_status"] = room.status.value
    view["host_id"] = room.host_id
    view["current_player_id"] = view.get("current_turn")
    view["team_names"] = room_state["team_names"]
    view["returned_to_lobby_player_ids"] = room_state["returned_to_lobby_player_ids"]
    if room.status == RoomStatus.GAME_SETUP:
        view["players"] = room_state["players"]
    return view


async def _broadcast_room_state(room_id: str):
    room_id = normalize_room_id(room_id)
    failed_connections = await connection_manager.broadcast(
        room_id,
        {"type": "ROOM_STATE_UPDATE", "payload": _get_room_state(room_id)},
    )
    _schedule_failed_disconnect_cleanups(room_id, failed_connections)
    if failed_connections:
        # One follow-up update reflects failed sockets as offline to healthy
        # clients, without recursively broadcasting on further failures.
        follow_up_failures = await connection_manager.broadcast(
            room_id,
            {"type": "ROOM_STATE_UPDATE", "payload": _get_room_state(room_id)},
        )
        _schedule_failed_disconnect_cleanups(room_id, follow_up_failures)
async def _broadcast_game_states(room_id: str):
    room_id = normalize_room_id(room_id)
    room = room_manager.get_room(room_id)
    if room.status not in (RoomStatus.GAME_SETUP, RoomStatus.IN_GAME) or room.engine is None:
        return
        
    connected_players = connection_manager.get_connected_player_ids(room_id)
    failed_players: list[str] = []
    for player_id in connected_players:
        view = room.engine.get_player_view(player_id)
        _enrich_game_view(room, view)
        encoded_view = jsonable_encoder(view)
        delivered = await connection_manager.send_to_player(
            room_id, player_id,
            {"type": "GAME_STATE_UPDATE", "payload": encoded_view},
        )
        if not delivered and connection_manager.get_connection(room_id, player_id) is None:
            failed_players.append(player_id)
    for player_id in failed_players:
        _schedule_disconnect_cleanup(room_id, player_id)
    if failed_players:
        await _broadcast_room_state(room_id)


def _schedule_trick_resolution(room_id: str) -> asyncio.Task | None:
    """Own one delayed, generation-guarded resolution task per room."""
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFound:
        return None
    if room.engine is None or room.engine.state.phase != GamePhase.TRICK_RESOLUTION:
        return None

    existing = _trick_resolution_tasks.get(room_id)
    if existing is not None and not existing.done():
        return existing

    room.trick_resolution_generation += 1
    generation = room.trick_resolution_generation
    match_generation = room.match_generation
    engine = room.engine
    expected_version = engine.state.version

    async def resolve_after_display() -> None:
        try:
            await asyncio.sleep(TRICK_DISPLAY_DURATION_SECONDS)
            current_task = asyncio.current_task()
            if _trick_resolution_tasks.get(room_id) is not current_task:
                return
            try:
                current_room = room_manager.get_room(room_id)
            except RoomNotFound:
                return
            if (
                current_room is not room
                or current_room.engine is not engine
                or current_room.match_generation != match_generation
                or current_room.trick_resolution_generation != generation
                or engine.state.version != expected_version
                or engine.state.phase != GamePhase.TRICK_RESOLUTION
            ):
                return

            engine.resolve_trick()
            await _broadcast_game_states(room_id)
            if engine.state.phase == GamePhase.FINAL_SCORE_DISPLAY:
                _schedule_final_score_display(room_id)
        except asyncio.CancelledError:
            return
        finally:
            if _trick_resolution_tasks.get(room_id) is asyncio.current_task():
                _trick_resolution_tasks.pop(room_id, None)

    task = asyncio.create_task(resolve_after_display())
    _trick_resolution_tasks[room_id] = task
    return task


def _schedule_final_score_display(room_id: str) -> asyncio.Task | None:
    """Transition a committed final scoreboard to its terminal result once."""
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFound:
        return None
    if (
        room.engine is None
        or room.engine.state.phase != GamePhase.FINAL_SCORE_DISPLAY
    ):
        return None

    existing = _final_score_display_tasks.get(room_id)
    if existing is not None and not existing.done():
        return existing

    room.final_score_display_generation += 1
    generation = room.final_score_display_generation
    trick_generation = room.trick_resolution_generation
    match_generation = room.match_generation
    engine = room.engine
    expected_version = engine.state.version

    async def transition_after_score_display() -> None:
        try:
            await asyncio.sleep(FINAL_SCORE_DISPLAY_DURATION_SECONDS)
            current_task = asyncio.current_task()
            if _final_score_display_tasks.get(room_id) is not current_task:
                return
            try:
                current_room = room_manager.get_room(room_id)
            except RoomNotFound:
                return
            if (
                current_room is not room
                or current_room.engine is not engine
                or current_room.match_generation != match_generation
                or current_room.final_score_display_generation != generation
                or current_room.trick_resolution_generation != trick_generation
                or engine.state.version != expected_version
                or engine.state.phase != GamePhase.FINAL_SCORE_DISPLAY
            ):
                return

            engine.finalize_game()
            await _broadcast_game_states(room_id)
        except asyncio.CancelledError:
            return
        finally:
            if _final_score_display_tasks.get(room_id) is asyncio.current_task():
                _final_score_display_tasks.pop(room_id, None)

    task = asyncio.create_task(transition_after_score_display())
    _final_score_display_tasks[room_id] = task
    return task


def _schedule_trump_reveal_display(room_id: str) -> asyncio.Task | None:
    """Own one delayed, generation-guarded trump reveal display task per room."""
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFound:
        return None
    if room.engine is None or room.engine.state.phase != GamePhase.TRUMP_REVEAL_DISPLAY:
        return None

    existing = _trump_reveal_display_tasks.get(room_id)
    if existing is not None and not existing.done():
        return existing

    room.trump_reveal_generation += 1
    generation = room.trump_reveal_generation
    match_generation = room.match_generation
    engine = room.engine
    expected_version = engine.state.version
    reveal_generation = engine.state.reveal_generation

    async def transition_after_reveal_display() -> None:
        try:
            await asyncio.sleep(TRUMP_REVEAL_DISPLAY_DURATION_SECONDS)
            current_task = asyncio.current_task()
            if _trump_reveal_display_tasks.get(room_id) is not current_task:
                return
            try:
                current_room = room_manager.get_room(room_id)
            except RoomNotFound:
                return
            if (
                current_room is not room
                or current_room.engine is not engine
                or current_room.match_generation != match_generation
                or current_room.trump_reveal_generation != generation
                or engine.state.version != expected_version
                or engine.state.reveal_generation != reveal_generation
                or engine.state.phase != GamePhase.TRUMP_REVEAL_DISPLAY
            ):
                return

            engine.complete_trump_reveal_display()
            await _broadcast_game_states(room_id)
            _schedule_hidden_card_return(room_id)
        except asyncio.CancelledError:
            return
        finally:
            if _trump_reveal_display_tasks.get(room_id) is asyncio.current_task():
                _trump_reveal_display_tasks.pop(room_id, None)

    task = asyncio.create_task(transition_after_reveal_display())
    _trump_reveal_display_tasks[room_id] = task
    return task


def _schedule_hidden_card_return(room_id: str) -> asyncio.Task | None:
    """Own one delayed, generation-guarded hidden card return task per room."""
    room_id = normalize_room_id(room_id)
    try:
        room = room_manager.get_room(room_id)
    except RoomNotFound:
        return None
    if room.engine is None or room.engine.state.phase != GamePhase.HIDDEN_CARD_RETURN:
        return None

    existing = _hidden_card_return_tasks.get(room_id)
    if existing is not None and not existing.done():
        return existing

    generation = room.trump_reveal_generation
    match_generation = room.match_generation
    engine = room.engine
    expected_version = engine.state.version
    reveal_generation = engine.state.reveal_generation

    async def transition_after_card_return() -> None:
        try:
            await asyncio.sleep(HIDDEN_CARD_RETURN_DURATION_SECONDS)
            current_task = asyncio.current_task()
            if _hidden_card_return_tasks.get(room_id) is not current_task:
                return
            try:
                current_room = room_manager.get_room(room_id)
            except RoomNotFound:
                return
            if (
                current_room is not room
                or current_room.engine is not engine
                or current_room.match_generation != match_generation
                or current_room.trump_reveal_generation != generation
                or engine.state.version != expected_version
                or engine.state.reveal_generation != reveal_generation
                or engine.state.phase != GamePhase.HIDDEN_CARD_RETURN
            ):
                return

            engine.complete_hidden_card_return()
            await _broadcast_game_states(room_id)
        except asyncio.CancelledError:
            return
        finally:
            if _hidden_card_return_tasks.get(room_id) is asyncio.current_task():
                _hidden_card_return_tasks.pop(room_id, None)

    task = asyncio.create_task(transition_after_card_return())
    _hidden_card_return_tasks[room_id] = task
    return task


def _development_test_push_enabled() -> bool:
    environment = os.getenv("MENDICOT_ENV", "production").strip().lower()
    enabled = os.getenv("MENDICOT_ENABLE_TEST_PUSH", "false").strip().lower()
    return environment in {"development", "test"} and enabled in {
        "1", "true", "yes", "on"
    }


def _push_session_is_active(
    room_id: str,
    player_id: str,
    token: str,
    websocket: WebSocket,
    room,
) -> bool:
    if not _session_token_matches_room(token, room_id, player_id):
        return False
    if connection_manager.get_connection(room_id, player_id) is not websocket:
        return False
    try:
        current_room = room_manager.get_room(room_id)
    except MendiCotError:
        return False
    return current_room is room and player_id in current_room.player_ids


async def _send_private_action_success(
    room_id: str, player_id: str, action: str, **safe_payload: object
) -> None:
    delivered = await connection_manager.send_to_player(
        room_id,
        player_id,
        {
            "type": "ACTION_SUCCESS",
            "payload": {"action": action, **safe_payload},
        },
    )
    if not delivered:
        await _handle_failed_direct_send(room_id, player_id)


@app.websocket("/ws/rooms/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, token: str = Query(...)):
    room_id = normalize_room_id(room_id)
    # 1. Validate session token
    if token not in session_tokens:
        await websocket.close(code=1008, reason="Invalid session token")
        return
        
    session_data = session_tokens[token]
    
    # 2. Validate room association
    if session_data["room_id"] != room_id:
        await websocket.close(code=1008, reason="Token does not match room")
        return
        
    player_id = session_data["player_id"]
    
    try:
        room = room_manager.get_room(room_id)
    except MendiCotError:
        await websocket.close(code=1008, reason="Room not found")
        return
    if player_id not in room.player_ids:
        invalidate_player_tokens(room_id, player_id)
        await websocket.close(code=1008, reason="Player session is no longer valid")
        return

    # 3. Register WebSocket
    await connection_manager.connect(room_id, player_id, websocket)
    room.set_player_online(player_id, True)
    _cancel_disconnect_cleanup(room_id, player_id)
    push_registrations.touch_session(room_id, player_id, token)

    try:
        # 4 & 5. Handle initial state sync
        if room.status == RoomStatus.WAITING:
            await _broadcast_room_state(room_id)
        elif room.status in (RoomStatus.GAME_SETUP, RoomStatus.IN_GAME):
            # The room-state broadcast is the authoritative online/offline
            # transition for every client; game state remains player-specific.
            await _broadcast_room_state(room_id)
            if room.engine:
                view = room.engine.get_player_view(player_id)
                _enrich_game_view(room, view)
                await connection_manager.send_to_player(
                    room_id, player_id,
                    {"type": "GAME_STATE_UPDATE", "payload": jsonable_encoder(view)}
                )
                if room.engine.state.phase == GamePhase.TRICK_RESOLUTION:
                    _schedule_trick_resolution(room_id)
                elif room.engine.state.phase == GamePhase.FINAL_SCORE_DISPLAY:
                    _schedule_final_score_display(room_id)
                elif room.engine.state.phase == GamePhase.TRUMP_REVEAL_DISPLAY:
                    # Reconnect: send current state, reuse existing timer.
                    _schedule_trump_reveal_display(room_id)
                elif room.engine.state.phase == GamePhase.HIDDEN_CARD_RETURN:
                    # Reconnect: send current state, reuse existing timer.
                    _schedule_hidden_card_return(room_id)
            
            await connection_manager.broadcast(
                room_id,
                {"type": "PLAYER_ONLINE", "payload": {"player_id": player_id}}
            )

        # Message loop
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            payload = data.get("payload", {})
            
            try:
                if action == "REGISTER_PUSH":
                    if not _push_session_is_active(
                        room_id, player_id, token, websocket, room
                    ):
                        raise InvalidSession(
                            "Session token is no longer valid for this room."
                        )
                    if not isinstance(payload, dict) or set(payload) != {
                        "registration_id", "enabled"
                    }:
                        raise ValueError(
                            "REGISTER_PUSH requires registration_id and enabled only."
                        )
                    push_registrations.register(
                        room_id, player_id, token,
                        payload["registration_id"], payload["enabled"],
                    )
                    await _send_private_action_success(
                        room_id, player_id, action, enabled=payload["enabled"]
                    )
                    continue

                elif action == "UPDATE_PUSH_PREFERENCE":
                    if not _push_session_is_active(
                        room_id, player_id, token, websocket, room
                    ):
                        raise InvalidSession(
                            "Session token is no longer valid for this room."
                        )
                    if not isinstance(payload, dict) or set(payload) != {"enabled"}:
                        raise ValueError(
                            "UPDATE_PUSH_PREFERENCE requires enabled only."
                        )
                    updated = push_registrations.update_preference(
                        room_id, player_id, token, payload["enabled"]
                    )
                    await _send_private_action_success(
                        room_id, player_id, action,
                        enabled=payload["enabled"],
                        status="updated" if updated else "no_registration",
                    )
                    continue

                elif action == "TEST_PUSH_NOTIFICATION":
                    if not _push_session_is_active(
                        room_id, player_id, token, websocket, room
                    ):
                        raise InvalidSession(
                            "Session token is no longer valid for this room."
                        )
                    if not isinstance(payload, dict) or payload:
                        raise ValueError(
                            "TEST_PUSH_NOTIFICATION does not accept a target payload."
                        )
                    if not _development_test_push_enabled():
                        test_status = "unavailable"
                    else:
                        targets = push_registrations.enabled_targets(room_id, player_id)
                        if not targets:
                            test_status = "no_registration"
                        else:
                            results = await firebase_push.send_player_turn_notifications(
                                targets,
                                remove_invalid=push_registrations.remove_target,
                            )
                            if PushSendResult.SENT in results:
                                test_status = "sent"
                            elif results and all(
                                result == PushSendResult.UNAVAILABLE
                                for result in results
                            ):
                                test_status = "unavailable"
                            else:
                                test_status = "failed"
                    await _send_private_action_success(
                        room_id, player_id, action, status=test_status
                    )
                    continue

                elif action == "START_GAME":
                    # The action envelope remains backward compatible, but the room
                    # configuration is the authoritative source for trump mode.
                    room.start_game(
                        player_id,
                        connection_manager.get_connected_player_ids(room_id),
                    )

                elif action == "SWITCH_TEAM":
                    team_id = (
                        payload.get("team_id")
                        if isinstance(payload, dict)
                        else None
                    )
                    room.switch_team(player_id, team_id)

                elif action == "RENAME_TEAM":
                    name = payload.get("name") if isinstance(payload, dict) else None
                    room.rename_team(player_id, name)
                    
                elif action == "SELECT_TRUMP_HIDER":
                    target_id = payload.get("player_id")
                    room.select_trump_hider(player_id, target_id)
                    
                elif action == "SELECT_FIRST_PLAYER":
                    target_id = payload.get("player_id")
                    room.select_first_player(player_id, target_id)

                elif action == "CANCEL_GAME_SETUP":
                    room.cancel_game_setup(player_id)
                    
                elif action == "DEAL_CARDS":
                    raise InvalidPhase(
                        "Cards are dealt only by the authoritative setup lifecycle."
                    )
                    
                elif action == "SELECT_HIDDEN_TRUMP":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    card_index = payload.get("card_index")
                    room.engine.select_hidden_card(player_id, card_index)
                    
                elif action == "COMPLETE_TRUMP_SETUP":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    room.engine.complete_hidden_trump_setup(player_id)
                    
                elif action == "PLAY_CARD":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    suit_str = payload.get("suit")
                    rank = payload.get("rank")
                    suit = Suit(suit_str)
                    card = Card(suit=suit, rank=rank)
                    room.engine.play_card(player_id, card)
                    
                elif action == "REVEAL_TRUMP":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    room.engine.reveal_trump(player_id)
                    
                elif action == "RESOLVE_TRICK":
                    raise InvalidPhase(
                        "Tricks resolve automatically after the display window."
                    )

                elif action == "RETURN_TO_LOBBY":
                    if not _session_token_matches_room(token, room_id, player_id):
                        raise InvalidSession(
                            "Session token is no longer valid for this room."
                        )
                    try:
                        was_reset = room.return_to_lobby(player_id)
                    except PlayerNotInRoom as exc:
                        await _send_action_error(
                            room_id,
                            player_id,
                            action,
                            "PLAYER_NOT_IN_ROOM",
                            str(exc),
                        )
                        continue
                    if was_reset:
                        # Only the final all-returned transition cancels the
                        # match lifecycle tasks. Partial returns keep the
                        # terminal engine and its timers untouched.
                        _cancel_room_lifecycle(room_id)

                elif action == "LEAVE_ROOM":
                    if room.status != RoomStatus.WAITING and not (
                        room.status == RoomStatus.IN_GAME and room.is_terminal()
                    ):
                        raise MendiCotError(
                            "Cannot leave an active game; game players are preserved."
                        )

                    # Reply while this socket is still registered, then make the
                    # leave irreversible before closing it.
                    await connection_manager.send_to_player(
                        room_id, player_id,
                        {"type": "ACTION_SUCCESS", "payload": {"action": action}},
                    )
                    connection_manager.disconnect(room_id, player_id, websocket)
                    _cancel_disconnect_cleanup(room_id, player_id)
                    await _remove_lobby_player(room_id, player_id)

                    await websocket.close(code=1000)
                    # Do not fall through to generic success/broadcast logic or
                    # process another message from a deliberately left socket.
                    return
                    
                else:
                    await _send_action_error(
                        room_id,
                        player_id,
                        action,
                        "UNKNOWN_ACTION",
                        f"Action {action} is unknown.",
                    )
                    continue

                # Action succeeded
                delivered = await connection_manager.send_to_player(
                    room_id,
                    player_id,
                    {"type": "ACTION_SUCCESS", "payload": {"action": action}},
                )
                if not delivered:
                    await _handle_failed_direct_send(room_id, player_id)
                
                # Broadcast state updates
                if delivered and action in (
                    "START_GAME",
                    "SWITCH_TEAM",
                    "RENAME_TEAM",
                    "CANCEL_GAME_SETUP",
                    "SELECT_FIRST_PLAYER",
                    "RETURN_TO_LOBBY",
                ):
                    await _broadcast_room_state(room_id)
                if room.status in (RoomStatus.GAME_SETUP, RoomStatus.IN_GAME):
                    await _broadcast_game_states(room_id)
                if (
                    action == "PLAY_CARD"
                    and room.engine is not None
                    and room.engine.state.phase == GamePhase.TRICK_RESOLUTION
                ):
                    # Start the timer only after every connected client has been
                    # sent the completed-trick snapshot.
                    _schedule_trick_resolution(room_id)
                if (
                    action == "REVEAL_TRUMP"
                    and room.engine is not None
                    and room.engine.state.phase == GamePhase.TRUMP_REVEAL_DISPLAY
                ):
                    # Start the reveal display timer after all clients receive
                    # the TRUMP_REVEAL_DISPLAY snapshot.
                    _schedule_trump_reveal_display(room_id)
                    
            except MendiCotError as e:
                await _send_action_error(
                    room_id,
                    player_id,
                    action,
                    _action_error_code(e),
                    str(e),
                )
            except ValueError as e:  # e.g. Suit(suit_str) fails
                await _send_action_error(
                    room_id,
                    player_id,
                    action,
                    "INVALID_PAYLOAD",
                    str(e),
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        # Keep malformed/interrupted WebSocket traffic from surfacing as an
        # unhandled ASGI error. The finally block still performs safe cleanup.
        try:
            await websocket.close(code=1011, reason="WebSocket processing failed")
        except Exception:
            pass
    finally:
        # Intentional leave and stale replaced sockets are no-ops because they
        # are no longer the exact registered socket.
        await _handle_socket_disconnect(room_id, player_id, websocket)
