import uuid
from typing import Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from mendicot.room_manager import RoomManager
from mendicot.room import RoomStatus
from mendicot.enums import GamePhase, Suit
from mendicot.models import Card
from mendicot.exceptions import MendiCotError

from .connection_manager import ConnectionManager
from .schemas import JoinRoomRequest, JoinRoomResponse, CreateRoomResponse

app = FastAPI(title="MendiCot API")

# Global State
room_manager = RoomManager()
connection_manager = ConnectionManager()

# session_token -> {"room_id": str, "player_id": str}
session_tokens: Dict[str, Dict[str, str]] = {}

@app.post("/api/rooms", response_model=CreateRoomResponse)
async def create_room():
    room_id = str(uuid.uuid4())[:8]
    room_manager.create_room(room_id)
    return CreateRoomResponse(room_id=room_id)

@app.post("/api/rooms/{room_id}/join", response_model=JoinRoomResponse)
async def join_room(room_id: str, request: JoinRoomRequest):
    try:
        room_manager.join_room(room_id, request.player_id, request.display_name)
    except MendiCotError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    session_token = str(uuid.uuid4())
    session_tokens[session_token] = {
        "room_id": room_id,
        "player_id": request.player_id
    }
    
    return JoinRoomResponse(
        room_id=room_id,
        player_id=request.player_id,
        session_token=session_token
    )

def _get_room_state(room_id: str) -> dict:
    room = room_manager.get_room(room_id)
    state = room.get_state()
    connected_players = connection_manager.get_connected_player_ids(room_id)
    
    for p in state["players"]:
        p["is_online"] = p["player_id"] in connected_players
        
    return state

async def _broadcast_room_state(room_id: str):
    await connection_manager.broadcast(
        room_id, 
        {"type": "ROOM_STATE_UPDATE", "payload": _get_room_state(room_id)}
    )

async def _broadcast_game_states(room_id: str):
    room = room_manager.get_room(room_id)
    if room.status != RoomStatus.IN_GAME or room.engine is None:
        return
        
    connected_players = connection_manager.get_connected_player_ids(room_id)
    for player_id in connected_players:
        view = room.engine.get_player_view(player_id)
        encoded_view = jsonable_encoder(view)
        await connection_manager.send_to_player(
            room_id, player_id, 
            {"type": "GAME_STATE_UPDATE", "payload": encoded_view}
        )

@app.websocket("/ws/rooms/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, token: str = Query(...)):
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

    # 3. Register WebSocket
    await connection_manager.connect(room_id, player_id, websocket)

    try:
        # 4 & 5. Handle initial state sync
        if room.status == RoomStatus.WAITING:
            await _broadcast_room_state(room_id)
        elif room.status == RoomStatus.IN_GAME:
            await connection_manager.send_to_player(
                room_id, player_id,
                {"type": "ROOM_STATE_UPDATE", "payload": _get_room_state(room_id)}
            )
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
                    hidden_trump_mode = payload.get("hidden_trump_mode", False)
                    room.start_game(player_id, hidden_trump_mode=hidden_trump_mode)
                    
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
                    room_manager.leave_room(room_id, player_id)
                    connection_manager.disconnect(room_id, player_id)
                    await websocket.close(code=1000)
                    await _broadcast_room_state(room_id)
                    break
                    
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
                if action in ("START_GAME", "LEAVE_ROOM"):
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
        connection_manager.disconnect(room_id, player_id)
        
        try:
            room = room_manager.get_room(room_id)
            await _broadcast_room_state(room_id)
            if room.status == RoomStatus.IN_GAME:
                await connection_manager.broadcast(
                    room_id,
                    {"type": "PLAYER_OFFLINE", "payload": {"player_id": player_id}}
                )
        except MendiCotError:
            pass
