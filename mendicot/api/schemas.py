from typing import Optional
from pydantic import BaseModel

class JoinRoomRequest(BaseModel):
    player_id: str
    display_name: str

class JoinRoomResponse(BaseModel):
    room_id: str
    player_id: str
    session_token: str

class CreateRoomResponse(BaseModel):
    room_id: str

class WsMessage(BaseModel):
    action: str
    payload: dict = {}
