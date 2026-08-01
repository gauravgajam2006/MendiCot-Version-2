import pytest
from mendicot.room import GameRoom, RoomStatus
from mendicot.room_manager import RoomManager
from mendicot.enums import GamePhase
from mendicot.exceptions import (
    DuplicatePlayer,
    RoomFull,
    GameAlreadyStarted,
    InvalidRoomSize,
    NotRoomHost,
    PlayerNotInRoom,
    RoomAlreadyExists,
    RoomNotFound,
)

# ----------------- GameRoom Tests -----------------

def mark_all_online(room):
    for player_id in room.player_ids:
        room.set_player_online(player_id, True)


def test_room_starts_empty():
    room = GameRoom("test_room")
    assert room.room_id == "test_room"
    assert room.player_count == 0
    assert room.host_id is None
    assert room.status == RoomStatus.WAITING

def test_first_player_becomes_host():
    room = GameRoom("test_room")
    room.add_player("P1", "Player 1")
    assert room.player_count == 1
    assert room.host_id == "P1"

def test_multiple_players_can_join():
    room = GameRoom("test_room")
    room.add_player("P1")
    room.add_player("P2")
    assert room.player_count == 2
    assert room.player_ids == ["P1", "P2"]
    assert room.host_id == "P1"

def test_duplicate_player_rejected():
    room = GameRoom("test_room")
    room.add_player("P1")
    with pytest.raises(DuplicatePlayer):
        room.add_player("P1")

def test_room_capacity_enforced():
    room = GameRoom("test_room")
    for i in range(1, 9):
        room.add_player(f"P{i}")
    
    assert room.player_count == 8
    
    with pytest.raises(RoomFull):
        room.add_player("P9")


def test_configured_room_capacity_is_enforced():
    room = GameRoom("four_player_room", configured_player_count=4, trump_mode="normal")
    for i in range(1, 5):
        room.add_player(f"P{i}")

    with pytest.raises(RoomFull, match="configured capacity \(4\)"):
        room.add_player("P5")


def test_room_state_includes_configuration():
    room = GameRoom("hidden_room", configured_player_count=6, trump_mode="hidden")
    assert room.get_state()["player_count"] == 6
    assert room.get_state()["trump_mode"] == "hidden"


def test_room_start_uses_stored_trump_mode():
    room = GameRoom("hidden_room", configured_player_count=4, trump_mode="hidden")
    for i in range(1, 5):
        room.add_player(f"P{i}")

    mark_all_online(room)
    room.start_game("P1")
    assert room.engine.state.hidden_trump_mode is True

def test_host_leaving_before_start_transfers_host():
    room = GameRoom("test_room")
    room.add_player("P1")
    room.add_player("P2")
    room.add_player("P3")
    
    room.remove_player("P1")
    assert room.host_id == "P2"
    assert room.player_count == 2
    
    room.remove_player("P2")
    assert room.host_id == "P3"
    
    room.remove_player("P3")
    assert room.host_id is None
    assert room.player_count == 0

def test_remove_unknown_player_raises_error():
    room = GameRoom("test_room")
    with pytest.raises(PlayerNotInRoom):
        room.remove_player("P1")

def test_only_host_can_start_game():
    room = GameRoom("test_room", configured_player_count=4)
    for i in range(1, 5):
        room.add_player(f"P{i}")
    
    with pytest.raises(NotRoomHost):
        room.start_game("P2")

    mark_all_online(room)
    room.start_game("P1")
    assert room.status == RoomStatus.IN_GAME

def test_cannot_start_with_invalid_player_count():
    room = GameRoom("test_room", configured_player_count=3)
    room.add_player("P1")
    room.add_player("P2")
    room.add_player("P3")
    
    with pytest.raises(InvalidRoomSize):
        room.start_game("P1")

def test_can_start_with_valid_player_counts():
    room_4 = GameRoom("room_4", configured_player_count=4)
    for i in range(1, 5): room_4.add_player(f"P{i}")
    mark_all_online(room_4)
    room_4.start_game("P1")
    assert room_4.status == RoomStatus.IN_GAME

    room_6 = GameRoom("room_6", configured_player_count=6)
    for i in range(1, 7): room_6.add_player(f"P{i}")
    mark_all_online(room_6)
    room_6.start_game("P1")
    assert room_6.status == RoomStatus.IN_GAME

    room_8 = GameRoom("room_8")
    for i in range(1, 9): room_8.add_player(f"P{i}")
    mark_all_online(room_8)
    room_8.start_game("P1")
    assert room_8.status == RoomStatus.IN_GAME

def test_game_cannot_start_twice():
    room = GameRoom("test_room", configured_player_count=4)
    for i in range(1, 5):
        room.add_player(f"P{i}")

    mark_all_online(room)
    room.start_game("P1")
    
    with pytest.raises(GameAlreadyStarted):
        room.start_game("P1")

def test_players_cannot_join_after_game_starts():
    room = GameRoom("test_room", configured_player_count=4)
    for i in range(1, 5):
        room.add_player(f"P{i}")

    mark_all_online(room)
    room.start_game("P1")
    
    with pytest.raises(GameAlreadyStarted):
        room.add_player("P5")

def test_players_cannot_leave_after_game_starts():
    room = GameRoom("test_room", configured_player_count=4)
    for i in range(1, 5):
        room.add_player(f"P{i}")

    mark_all_online(room)
    room.start_game("P1")
    
    with pytest.raises(GameAlreadyStarted):
        room.remove_player("P1")
    with pytest.raises(GameAlreadyStarted):
        room.remove_player("P2")


# ----------------- RoomManager Tests -----------------

def test_manager_create_room():
    manager = RoomManager()
    room = manager.create_room("room_1")
    assert room.room_id == "room_1"

def test_manager_reject_duplicate_room():
    manager = RoomManager()
    manager.create_room("room_1")
    with pytest.raises(RoomAlreadyExists):
        manager.create_room("room_1")

def test_manager_get_room():
    manager = RoomManager()
    created_room = manager.create_room("room_1")
    fetched_room = manager.get_room("room_1")
    assert created_room is fetched_room

def test_manager_get_missing_room():
    manager = RoomManager()
    with pytest.raises(RoomNotFound):
        manager.get_room("non_existent")

def test_manager_delete_room():
    manager = RoomManager()
    manager.create_room("room_1")
    manager.delete_room("room_1")
    with pytest.raises(RoomNotFound):
        manager.get_room("room_1")

def test_manager_delete_missing_room():
    manager = RoomManager()
    with pytest.raises(RoomNotFound):
        manager.delete_room("room_1")

def test_manager_join_and_leave_room():
    manager = RoomManager()
    manager.create_room("room_1")
    
    manager.join_room("room_1", "P1")
    room = manager.get_room("room_1")
    assert room.player_ids == ["P1"]
    
    manager.leave_room("room_1", "P1")
    assert room.player_count == 0


# ----------------- Engine Isolation Tests -----------------

def test_engine_isolation():
    manager = RoomManager()
    room_a = manager.create_room("room_a", player_count=4)
    room_b = manager.create_room("room_b", player_count=4)
    
    # Populate Room A
    for i in range(1, 5):
        manager.join_room("room_a", f"A{i}")
        
    # Populate Room B
    for i in range(1, 5):
        manager.join_room("room_b", f"B{i}")

    mark_all_online(room_a)
    mark_all_online(room_b)
    room_a.start_game("A1")
    room_b.start_game("B1")
    
    # Verify engines are distinct
    assert room_a.engine is not room_b.engine
    
    # Verify GameStates are distinct
    state_a = room_a.engine.state
    state_b = room_b.engine.state
    assert state_a is not state_b
    
    assert state_a.game_id == "room_a"
    assert state_b.game_id == "room_b"
    
    assert state_a.host_id == "A1"
    assert state_b.host_id == "B1"
    
    # Check that modifying Room A does not affect Room B
    room_a.engine.deal_cards()
    assert state_a.phase == GamePhase.PLAYING
    assert state_b.phase == GamePhase.CREATED
