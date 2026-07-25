import pytest
from fastapi.testclient import TestClient
from mendicot.api.routes import app, room_manager, connection_manager, session_tokens
from mendicot.enums import GamePhase

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_state():
    """Reset the global state before each test."""
    room_manager._rooms.clear()
    connection_manager.active_connections.clear()
    session_tokens.clear()
    yield

def test_create_and_join_room():
    # Create room
    response = client.post("/api/rooms")
    assert response.status_code == 200
    room_id = response.json()["room_id"]
    
    # Join room
    response = client.post(f"/api/rooms/{room_id}/join", json={"player_id": "P1", "display_name": "Alice"})
    assert response.status_code == 200
    data = response.json()
    assert data["room_id"] == room_id
    assert data["player_id"] == "P1"
    assert "session_token" in data
    
def test_websocket_connect_and_broadcast():
    # Create and join
    r1 = client.post("/api/rooms")
    room_id = r1.json()["room_id"]
    r2 = client.post(f"/api/rooms/{room_id}/join", json={"player_id": "P1", "display_name": "Alice"})
    token1 = r2.json()["session_token"]
    
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={token1}") as ws1:
        # Expect ROOM_STATE_UPDATE
        data = ws1.receive_json()
        assert data["type"] == "ROOM_STATE_UPDATE"
        assert len(data["payload"]["players"]) == 1
        assert data["payload"]["players"][0]["is_online"] is True
        
        # Second player joins
        r3 = client.post(f"/api/rooms/{room_id}/join", json={"player_id": "P2", "display_name": "Bob"})
        token2 = r3.json()["session_token"]
        
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={token2}") as ws2:
            # P2 gets room state
            d2 = ws2.receive_json()
            assert d2["type"] == "ROOM_STATE_UPDATE"
            
            # P1 should also get the updated room state because P2 connected
            # Oh wait, P2 connecting triggers ROOM_STATE_UPDATE broadcast.
            # P1 gets it.
            d1 = ws1.receive_json()
            assert d1["type"] == "ROOM_STATE_UPDATE"
            assert len(d1["payload"]["players"]) == 2

def test_invalid_token_rejected():
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/rooms/ANY?token=invalid_token"):
            pass

def test_start_game_flow():
    r1 = client.post("/api/rooms")
    room_id = r1.json()["room_id"]
    
    # Join 4 players
    tokens = []
    for i in range(4):
        r = client.post(f"/api/rooms/{room_id}/join", json={"player_id": f"P{i+1}", "display_name": f"P{i+1}"})
        tokens.append(r.json()["session_token"])
        
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={tokens[0]}") as ws1:
        # Ignore initial broadcasts
        ws1.receive_json()
        
        # Host starts game
        ws1.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        
        # Expect ACTION_SUCCESS
        resp = ws1.receive_json()
        assert resp["type"] == "ACTION_SUCCESS"
        assert resp["payload"]["action"] == "START_GAME"
        
        # Expect ROOM_STATE_UPDATE (because room status changed)
        resp = ws1.receive_json()
        assert resp["type"] == "ROOM_STATE_UPDATE"
        assert resp["payload"]["status"] == "IN_GAME"
        
        # Expect GAME_STATE_UPDATE
        resp = ws1.receive_json()
        assert resp["type"] == "GAME_STATE_UPDATE"
        assert resp["payload"]["phase"] == GamePhase.CREATED.value
        
def test_error_handling_invalid_action():
    r1 = client.post("/api/rooms")
    room_id = r1.json()["room_id"]
    r2 = client.post(f"/api/rooms/{room_id}/join", json={"player_id": "P1", "display_name": "Alice"})
    token1 = r2.json()["session_token"]
    
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={token1}") as ws1:
        ws1.receive_json() # ROOM_STATE_UPDATE
        
        # Send unknown action
        ws1.send_json({"action": "MADE_UP_ACTION"})
        resp = ws1.receive_json()
        assert resp["type"] == "ERROR"
        assert resp["payload"]["code"] == "UNKNOWN_ACTION"
        
        # Send game action when not in game
        ws1.send_json({"action": "PLAY_CARD", "payload": {"suit": "HEARTS", "rank": 14}})
        resp = ws1.receive_json()
        assert resp["type"] == "ERROR"
        assert resp["payload"]["code"] == "MendiCotError"
