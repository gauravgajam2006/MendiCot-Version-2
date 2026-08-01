import asyncio
import hashlib
import hmac
import os
import uuid
from typing import Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware

from mendicot.room_manager import RoomManager
from mendicot.room import RoomStatus
from mendicot.enums import GamePhase, Suit
from mendicot.models import Card
from mendicot.exceptions import MendiCotError, RoomNotFound
from mendicot.room_ids import normalize_room_id

from .connection_manager import ConnectionManager
from .schemas import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomRequest,
    JoinRoomResponse,
    ValidateSessionRequest,
    ValidateSessionResponse,
)

app = FastAPI(title="MendiCot API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global State
room_manager = RoomManager()
connection_manager = ConnectionManager()

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
_disconnect_cleanup_tasks: Dict[tuple[str, str], asyncio.Task] = {}


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
    candidates = [p.player_id for p in room._players if p.player_id != excluded_player_id]
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
    if room.status != RoomStatus.WAITING or player_id not in room.player_ids:
        return False
    removing_host = room.host_id == player_id
    next_host_id = _next_host_id(room, player_id) if removing_host else None
    room_manager.leave_room(room_id, player_id)
    if removing_host:
        room.host_id = next_host_id
    invalidate_player_tokens(room_id, player_id)
    if room.player_count == 0:
        _cancel_room_disconnect_cleanups(room_id)
        invalidate_room_tokens(room_id)
        connection_manager.clear_room(room_id)
        room_manager.delete_room(room_id)
        return True
    await _broadcast_room_state(room_id)
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
        _schedule_disconnect_cleanup(room_id, player_id)
        await _broadcast_room_state(room_id)
        if room.status == RoomStatus.IN_GAME:
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
            _schedule_disconnect_cleanup(room_id, player_id)

def _get_room_state(room_id: str) -> dict:
    room_id = normalize_room_id(room_id)
    room = room_manager.get_room(room_id)
    state = room.get_state()
    connected_players = connection_manager.get_connected_player_ids(room_id)
    
    for p in state["players"]:
        p["is_online"] = p["player_id"] in connected_players
        
    return state

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
    if room.status != RoomStatus.IN_GAME or room.engine is None:
        return
        
    connected_players = connection_manager.get_connected_player_ids(room_id)
    failed_players: list[str] = []
    for player_id in connected_players:
        view = room.engine.get_player_view(player_id)
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
    _cancel_disconnect_cleanup(room_id, player_id)

    try:
        # 4 & 5. Handle initial state sync
        if room.status == RoomStatus.WAITING:
            await _broadcast_room_state(room_id)
        elif room.status == RoomStatus.IN_GAME:
            # The room-state broadcast is the authoritative online/offline
            # transition for every client; game state remains player-specific.
            await _broadcast_room_state(room_id)
            if room.engine:
                view = room.engine.get_player_view(player_id)
                await connection_manager.send_to_player(
                    room_id, player_id,
                    {"type": "GAME_STATE_UPDATE", "payload": jsonable_encoder(view)}
                )
            
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
                if action == "START_GAME":
                    # The action envelope remains backward compatible, but the room
                    # configuration is the authoritative source for trump mode.
                    room.start_game(player_id)
                    
                elif action == "SELECT_TRUMP_HIDER":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    target_id = payload.get("player_id")
                    room.engine.select_trump_hider(player_id, target_id)
                    
                elif action == "SELECT_FIRST_PLAYER":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    target_id = payload.get("player_id")
                    room.engine.select_first_player(player_id, target_id)
                    
                elif action == "DEAL_CARDS":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    if player_id != room.host_id:
                        raise MendiCotError("Only host can deal cards.")
                    room.engine.deal_cards()
                    
                elif action == "SELECT_HIDDEN_TRUMP":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    card_index = payload.get("card_index")
                    room.engine.select_hidden_card(player_id, card_index)
                    
                elif action == "COMPLETE_TRUMP_SETUP":
                    if room.engine is None: raise MendiCotError("Game not started.")
                    if player_id != room.engine.state.trump_state.trump_hider_id:
                        raise MendiCotError("Not the selected trump hider.")
                    room.engine.complete_hidden_trump_setup()
                    
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
                    if room.engine is None: raise MendiCotError("Game not started.")
                    room.engine.resolve_trick()
                    
                elif action == "LEAVE_ROOM":
                    if room.status != RoomStatus.WAITING:
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
                    await connection_manager.send_to_player(
                        room_id, player_id,
                        {"type": "ERROR", "payload": {"code": "UNKNOWN_ACTION", "message": f"Action {action} is unknown."}}
                    )
                    continue

                # Action succeeded
                await connection_manager.send_to_player(
                    room_id, player_id,
                    {"type": "ACTION_SUCCESS", "payload": {"action": action}}
                )
                
                # Broadcast state updates
                if action == "START_GAME":
                    await _broadcast_room_state(room_id)
                if room.status == RoomStatus.IN_GAME:
                    await _broadcast_game_states(room_id)
                    
            except MendiCotError as e:
                error_code = e.__class__.__name__
                # Special handling for enum conversion errors etc. could go here
                await connection_manager.send_to_player(
                    room_id, player_id,
                    {"type": "ERROR", "payload": {"code": error_code, "message": str(e)}}
                )
            except ValueError as e: # e.g. Suit(suit_str) fails
                 await connection_manager.send_to_player(
                    room_id, player_id,
                    {"type": "ERROR", "payload": {"code": "INVALID_PAYLOAD", "message": str(e)}}
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
