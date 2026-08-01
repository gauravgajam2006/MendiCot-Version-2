import pytest
from fastapi.testclient import TestClient
from typing import List, Dict
from contextlib import ExitStack
import time

from mendicot.api.routes import app, room_manager, connection_manager, session_tokens
from mendicot.enums import GamePhase, Suit

client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state before each test."""
    room_manager._rooms.clear()
    connection_manager.active_connections.clear()
    session_tokens.clear()
    yield

def setup_room_with_players(
    num_players: int = 4, trump_mode: str = "normal"
) -> tuple[str, List[Dict[str, str]]]:
    """Helper to create a room and join N players. Returns (room_id, list_of_player_data)."""
    configured_player_count = max(num_players, 4)
    resp = client.post(
        "/api/rooms",
        json={"player_count": configured_player_count, "trump_mode": trump_mode},
    )
    room_id = resp.json()["room_id"]
    
    players = []
    for i in range(num_players):
        player_id = f"P{i+1}"
        display_name = f"Player {i+1}"
        join_resp = client.post(f"/api/rooms/{room_id}/join", json={"player_id": player_id, "display_name": display_name})
        players.append(join_resp.json())
        
    return room_id, players

def wait_for_message(ws, msg_type: str, max_messages=20):
    msgs = []
    for _ in range(max_messages):
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == msg_type:
            return msg
    raise AssertionError(f"Message {msg_type} not received. Received: {[m['type'] for m in msgs]}")

def wait_for_game_phase(ws, phase: GamePhase, max_messages=20):
    msgs = []
    for _ in range(max_messages):
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == "GAME_STATE_UPDATE" and msg["payload"].get("phase") == phase.value:
            return msg
    raise AssertionError(f"Did not reach phase {phase.value}. Received: {[m['type'] for m in msgs]}")

# --- 1. ROOM CREATION AND JOINING ---
@pytest.mark.parametrize("num_players", [4, 6, 8])
def test_ws_join_lifecycle_4_6_8_players(num_players):
    room_id, players = setup_room_with_players(num_players)
    
    with ExitStack() as stack:
        websockets = []
        for p in players:
            ws = stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}?token={p['session_token']}"))
            websockets.append(ws)
            wait_for_message(ws, "ROOM_STATE_UPDATE")
            
        assert len(websockets) == num_players

def test_invalid_tokens_rejected():
    room_id, players = setup_room_with_players(4)
    # Invalid token
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/rooms/{room_id}?token=invalid_uuid"):
            pass
            
    # Token from another room
    room_id2, players2 = setup_room_with_players(4)
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={players2[0]['session_token']}"):
            pass

# --- 2. LOBBY SYNCHRONIZATION ---
def test_ws_lobby_state_broadcasts():
    room_id, players = setup_room_with_players(2)
    
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as ws1:
        state = ws1.receive_json()
        assert state["type"] == "ROOM_STATE_UPDATE"
        assert state["payload"]["room_id"] == room_id
        assert state["payload"]["host_id"] == players[0]["player_id"]
        assert state["payload"]["status"] == "WAITING"
        assert len(state["payload"]["players"]) == 2
        assert state["payload"]["players"][0]["is_online"] is True
        assert state["payload"]["players"][1]["is_online"] is False

def test_ws_lobby_disconnect_reconnect():
    room_id, players = setup_room_with_players(1)
    
    # Connect
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as ws1:
        wait_for_message(ws1, "ROOM_STATE_UPDATE")
    
    # Wait a bit to ensure async disconnect handler runs in the test environment
    time.sleep(0.1)
    
    # Room still exists, player still in room
    room = room_manager.get_room(room_id)
    assert len(room.player_ids) == 1
    
    # Reconnect
    with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as ws2:
        state = wait_for_message(ws2, "ROOM_STATE_UPDATE")
        assert state["payload"]["players"][0]["is_online"] is True

# --- 3. HOST PERMISSIONS ---
def test_ws_host_permissions():
    room_id, players = setup_room_with_players(4)

    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players
        ]
        host_ws, non_host_ws = websockets[:2]

        # Non-host tries to start game
        non_host_ws.send_json(
            {"action": "START_GAME", "payload": {"hidden_trump_mode": False}}
        )
        err = wait_for_message(non_host_ws, "ERROR")
        assert err["payload"]["code"] == "HOST_ONLY"
        assert err["payload"]["action"] == "START_GAME"

        # Host starts game
        host_ws.send_json(
            {"action": "START_GAME", "payload": {"hidden_trump_mode": False}}
        )
        wait_for_message(host_ws, "ACTION_SUCCESS")

        # Non-host tries DEAL_CARDS
        non_host_ws.send_json({"action": "DEAL_CARDS"})
        err = wait_for_message(non_host_ws, "ERROR")
        assert err["payload"]["code"] == "MendiCotError"
        assert "Only host can deal cards" in err["payload"]["message"]

# --- 4. NORMAL TRUMP MODE OVER WEBSOCKET ---
def test_ws_normal_trump_gameplay():
    room_id, players = setup_room_with_players(4)
    
    with ExitStack() as stack:
        websockets = []
        for p in players:
            websockets.append(stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}?token={p['session_token']}")))
            
        host_ws = websockets[0]
        
        # START_GAME
        host_ws.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(host_ws, "ACTION_SUCCESS")
            
        # SELECT FIRST PLAYER (P1)
        host_ws.send_json({"action": "SELECT_FIRST_PLAYER", "payload": {"player_id": players[0]["player_id"]}})
        wait_for_message(host_ws, "ACTION_SUCCESS")

        # DEAL CARDS
        host_ws.send_json({"action": "DEAL_CARDS"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        hands = []
        for i, ws in enumerate(websockets):
            update = wait_for_game_phase(ws, GamePhase.PLAYING)
            my_hand = update["payload"]["hands"][players[i]["player_id"]]
            assert len(my_hand) == 12
            hands.append(my_hand)
            
        # P1 plays
        p1_card = hands[0][0]
        websockets[0].send_json({"action": "PLAY_CARD", "payload": {"suit": p1_card["suit"], "rank": p1_card["rank"]}})
        wait_for_message(websockets[0], "ACTION_SUCCESS")
            
        # P2, P3, P4 play
        for i in range(1, 4):
            # Find a valid card (same suit if possible)
            valid_card = None
            for c in hands[i]:
                if c["suit"] == p1_card["suit"]:
                    valid_card = c
                    break
            if not valid_card:
                valid_card = hands[i][0]
                
            websockets[i].send_json({"action": "PLAY_CARD", "payload": {"suit": valid_card["suit"], "rank": valid_card["rank"]}})
            wait_for_message(websockets[i], "ACTION_SUCCESS")
                
        # Resolve trick
        host_ws.send_json({"action": "RESOLVE_TRICK"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        # Verify trick resolved and back to playing
        for ws in websockets:
            # First wait for the SUCCESS or state update
            # We can wait for GAME_STATE_UPDATE where current_trick is empty
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "GAME_STATE_UPDATE" and len(msg["payload"]["current_trick"]["played_cards"]) == 0 and msg["payload"]["phase"] == GamePhase.PLAYING.value:
                    break
            else:
                pytest.fail("Did not receive resolved trick state")

# --- 5. HIDDEN TRUMP MODE OVER WEBSOCKET ---
@pytest.mark.parametrize("num_players", [4, 6, 8])
def test_ws_hidden_trump_lifecycle(num_players):
    room_id, players = setup_room_with_players(num_players, trump_mode="hidden")

    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players
        ]
        host_ws, hider_ws, p3_ws = websockets[:3]
         
        # START_GAME hidden
        host_ws.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": True}})
        wait_for_message(host_ws, "ACTION_SUCCESS")

        # SELECT TRUMP HIDER
        host_ws.send_json({"action": "SELECT_TRUMP_HIDER", "payload": {"player_id": players[1]["player_id"]}})
        wait_for_message(host_ws, "ACTION_SUCCESS")
            
        # SELECT FIRST PLAYER
        host_ws.send_json(
            {
                "action": "SELECT_FIRST_PLAYER",
                "payload": {"player_id": players[0]["player_id"]},
            }
        )
        wait_for_message(host_ws, "ACTION_SUCCESS")
            
        # DEAL CARDS
        host_ws.send_json({"action": "DEAL_CARDS"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        for ws in [host_ws, hider_ws, p3_ws]: 
            wait_for_game_phase(ws, GamePhase.HIDDEN_TRUMP_SELECTION)
            
        # Hider selects trump
        hider_ws.send_json({"action": "SELECT_HIDDEN_TRUMP", "payload": {"card_index": 0}})
        wait_for_message(hider_ws, "ACTION_SUCCESS")
        
        p3_state = wait_for_game_phase(p3_ws, GamePhase.HIDDEN_TRUMP_REVEAL)
        assert p3_state["payload"]["trump_state"]["suit"] is None
        
        hider_state = wait_for_game_phase(hider_ws, GamePhase.HIDDEN_TRUMP_REVEAL)
        assert hider_state["payload"]["trump_state"]["suit"] is not None
        
        # Hider completes setup
        hider_ws.send_json({"action": "COMPLETE_TRUMP_SETUP"})
        wait_for_message(hider_ws, "ACTION_SUCCESS")
        
        for ws in [host_ws, hider_ws, p3_ws]:
            wait_for_game_phase(ws, GamePhase.PLAYING)

# --- 6. 6-PLAYER AND 8-PLAYER GAMEPLAY ---
def test_ws_multiplayer_gameplay_6p():
    room_id, players = setup_room_with_players(6)
    with ExitStack() as stack:
        websockets = []
        for p in players:
            websockets.append(stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}?token={p['session_token']}")))
            
        host_ws = websockets[0]
        host_ws.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        host_ws.send_json({"action": "DEAL_CARDS"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        hands = []
        for i, ws in enumerate(websockets):
            st = wait_for_game_phase(ws, GamePhase.PLAYING)
            hands.append(st["payload"]["hands"][players[i]["player_id"]])
            assert len(st["payload"]["hands"][players[i]["player_id"]]) == 8 
            
        # P1 plays
        p1_card = hands[0][0]
        websockets[0].send_json({"action": "PLAY_CARD", "payload": {"suit": p1_card["suit"], "rank": p1_card["rank"]}})
        wait_for_message(websockets[0], "ACTION_SUCCESS")
        
        # P2 to P6 play
        for i in range(1, 6):
            valid_card = None
            for c in hands[i]:
                if c["suit"] == p1_card["suit"]:
                    valid_card = c
                    break
            if not valid_card:
                valid_card = hands[i][0]
                
            websockets[i].send_json({"action": "PLAY_CARD", "payload": {"suit": valid_card["suit"], "rank": valid_card["rank"]}})
            wait_for_message(websockets[i], "ACTION_SUCCESS")
            
        host_ws.send_json({"action": "RESOLVE_TRICK"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        for ws in websockets: 
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "GAME_STATE_UPDATE" and len(msg["payload"]["current_trick"]["played_cards"]) == 0 and msg["payload"]["phase"] == GamePhase.PLAYING.value:
                    break
            else:
                pytest.fail("Did not receive resolved trick state")

def test_ws_multiplayer_gameplay_8p():
    room_id, players = setup_room_with_players(8)
    with ExitStack() as stack:
        websockets = []
        for p in players:
            websockets.append(stack.enter_context(client.websocket_connect(f"/ws/rooms/{room_id}?token={p['session_token']}")))
            
        host_ws = websockets[0]
        host_ws.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        host_ws.send_json({"action": "DEAL_CARDS"})
        wait_for_message(host_ws, "ACTION_SUCCESS")
        
        for i, ws in enumerate(websockets):
            st = wait_for_game_phase(ws, GamePhase.PLAYING)
            assert len(st["payload"]["hands"][players[i]["player_id"]]) == 6

# --- 7. PRIVATE STATE ISOLATION ---
def test_ws_private_state_isolation():
    room_id, players = setup_room_with_players(4)
    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players
        ]
        ws1, ws2 = websockets[:2]

        ws1.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(ws1, "ACTION_SUCCESS")
        ws1.send_json({"action": "DEAL_CARDS"})
        wait_for_message(ws1, "ACTION_SUCCESS")
        
        s1 = wait_for_game_phase(ws1, GamePhase.PLAYING)
        s2 = wait_for_game_phase(ws2, GamePhase.PLAYING)
        
        assert s1["payload"]["hands"][players[0]["player_id"]] != s2["payload"]["hands"][players[1]["player_id"]]
        assert s1["payload"]["current_turn"] == s2["payload"]["current_turn"]

# --- 8. ROOM ISOLATION ---
def test_ws_room_isolation():
    r1, p1 = setup_room_with_players(4)
    r2, p2 = setup_room_with_players(4)

    with ExitStack() as stack:
        for player in p1[1:]:
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{r1}?token={player['session_token']}"
                )
            )
        for player in p2[1:]:
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{r2}?token={player['session_token']}"
                )
            )
        ws1 = stack.enter_context(
            client.websocket_connect(f"/ws/rooms/{r1}?token={p1[0]['session_token']}")
        )
        ws2 = stack.enter_context(
            client.websocket_connect(f"/ws/rooms/{r2}?token={p2[0]['session_token']}")
        )
        wait_for_message(ws2, "ROOM_STATE_UPDATE")

        ws1.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(ws1, "ACTION_SUCCESS")
        
        # Room B should not have any pending messages.
        assert ws2._receive_queue.empty()

# --- 9. RECONNECT DURING ACTIVE GAME ---
def test_ws_reconnect_during_game():
    room_id, players = setup_room_with_players(4)

    with ExitStack() as stack:
        connected_players = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players[1:]
        ]
        ws2 = connected_players[0]
        # We need ws1 outside context manager so we can close it
        ws1 = client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}")
        ws1 = ws1.__enter__()
        
        ws1.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(ws1, "ACTION_SUCCESS")
        ws1.send_json({"action": "DEAL_CARDS"})
        wait_for_message(ws1, "ACTION_SUCCESS")
        
        ws1.close()
        
        # P2 receives PLAYER_OFFLINE
        msg = wait_for_message(ws2, "PLAYER_OFFLINE")
        assert msg["payload"]["player_id"] == players[0]["player_id"]
        
        # Reconnect P1
        with client.websocket_connect(f"/ws/rooms/{room_id}?token={players[0]['session_token']}") as ws1_reconnect:
            st1 = wait_for_message(ws1_reconnect, "ROOM_STATE_UPDATE")
            st2 = wait_for_message(ws1_reconnect, "GAME_STATE_UPDATE")
            assert len(st2["payload"]["hands"][players[0]["player_id"]]) == 12
            
            # P2 receives PLAYER_ONLINE
            wait_for_message(ws2, "PLAYER_ONLINE")

# --- 10. ERROR CONTRACT ---
def test_ws_error_contract():
    room_id, players = setup_room_with_players(4)
    with ExitStack() as stack:
        websockets = [
            stack.enter_context(
                client.websocket_connect(
                    f"/ws/rooms/{room_id}?token={player['session_token']}"
                )
            )
            for player in players
        ]
        ws1, ws2 = websockets[:2]
             
        # Unknown action
        ws1.send_json({"action": "INVALID_ACTION"})
        err = wait_for_message(ws1, "ERROR")
        assert err["payload"]["code"] == "UNKNOWN_ACTION"
        
        # Start game
        ws1.send_json({"action": "START_GAME", "payload": {"hidden_trump_mode": False}})
        wait_for_message(ws1, "ACTION_SUCCESS")
        
        # Invalid payload
        ws1.send_json({"action": "PLAY_CARD", "payload": {"suit": "INVALID", "rank": 99}})
        err2 = wait_for_message(ws1, "ERROR")
        assert err2["payload"]["code"] == "INVALID_PAYLOAD"
        
        # Wrong turn/phase
        ws2.send_json({"action": "PLAY_CARD", "payload": {"suit": "HEARTS", "rank": 2}})
        err3 = wait_for_message(ws2, "ERROR")
        assert err3["payload"]["code"] == "INVALID_PHASE"
