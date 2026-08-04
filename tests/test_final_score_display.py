import asyncio

import pytest

from mendicot.api import routes
from mendicot.enums import GamePhase, Rank, Suit
from mendicot.models import Card


class Socket:
    def __init__(self):
        self.messages = []

    async def accept(self):
        pass

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, *args, **kwargs):
        pass


def clear_state():
    for task in list(routes._disconnect_cleanup_tasks.values()):
        task.cancel()
    routes._disconnect_cleanup_tasks.clear()
    for task in list(routes._trick_resolution_tasks.values()):
        task.cancel()
    routes._trick_resolution_tasks.clear()
    for task in list(routes._final_score_display_tasks.values()):
        task.cancel()
    routes._final_score_display_tasks.clear()
    routes.room_manager._rooms.clear()
    routes.connection_manager.active_connections.clear()
    routes.session_tokens.clear()
    routes.invalidated_session_tokens.clear()


@pytest.fixture(autouse=True)
def reset_state():
    clear_state()
    yield
    clear_state()


def create_final_trick_room(room_id, player_count, trump_mode="normal", draw=False):
    room = routes.room_manager.create_room(room_id, player_count, trump_mode)
    for index in range(player_count):
        player_id = f"P{index + 1}"
        room.add_player(player_id, f"Player {index + 1}")
        room.set_player_online(player_id, True)
    room.start_game("P1", set(room.player_ids))
    if trump_mode == "hidden":
        room.select_trump_hider("P1", "P1")
    room.select_first_player("P1", "P1")

    state = room.engine.state
    if draw:
        state.captured_mendis["TeamA"] = [Suit.SPADES]
        state.teams["TeamA"].tens_captured = 1
        state.teams["TeamA"].tricks_won = 1

    state.phase = GamePhase.PLAYING
    state.current_turn = "P1"
    state.hands = {
        player.player_id: [
            Card(
                Suit.HEARTS,
                Rank.TEN
                if player.player_id == "P1"
                else Rank.ACE
                if player.player_id == "P2"
                else Rank.THREE,
            )
        ]
        for player in state.players
    }
    for _ in range(player_count):
        player_id = state.current_turn
        room.engine.play_card(player_id, state.hands[player_id][0])
    assert state.phase == GamePhase.TRICK_RESOLUTION
    return room


async def connect_sockets(room):
    sockets = {}
    for player_id in room.player_ids:
        socket = Socket()
        await routes.connection_manager.connect(room.room_id, player_id, socket)
        sockets[player_id] = socket
    return sockets


def game_phases(socket):
    return [
        message["payload"]["phase"]
        for message in socket.messages
        if message["type"] == "GAME_STATE_UPDATE"
    ]


@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_final_lifecycle_broadcasts_scoreboard_then_terminal_result(
    monkeypatch, player_count, trump_mode
):
    async def scenario():
        room = create_final_trick_room(
            f"final-{player_count}-{trump_mode}", player_count, trump_mode
        )
        sockets = await connect_sockets(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.01)

        trick_task = routes._schedule_trick_resolution(room.room_id)
        await trick_task

        state = room.engine.state
        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
        assert state.final_result == "TeamB"
        assert state.current_trick.played_cards == []
        assert state.teams["TeamB"].tricks_won == 1
        assert state.teams["TeamB"].tens_captured == 1
        assert state.captured_mendis == {
            "TeamA": [],
            "TeamB": [Suit.HEARTS],
        }

        scoreboard = sockets["P1"].messages[-1]["payload"]
        assert scoreboard["phase"] == GamePhase.FINAL_SCORE_DISPLAY.value
        assert scoreboard["final_result"] == "TeamB"
        assert scoreboard["current_trick"]["played_cards"] == []
        assert scoreboard["captured_mendis"] == {
            "TeamA": [],
            "TeamB": ["HEARTS"],
        }
        assert scoreboard["teams"]["TeamB"]["tricks_won"] == 1
        assert scoreboard["teams"]["TeamB"]["tens_captured"] == 1
        assert scoreboard["team_names"] == {
            "TeamA": "Team Maroon",
            "TeamB": "Team Gold",
        }

        final_task = routes._final_score_display_tasks[room.room_id]
        assert routes._schedule_final_score_display(room.room_id) is final_task
        await asyncio.sleep(0)
        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
        assert GamePhase.GAME_OVER.value not in game_phases(sockets["P1"])

        await final_task
        assert state.phase == GamePhase.GAME_OVER
        assert game_phases(sockets["P1"]).count(GamePhase.GAME_OVER.value) == 1
        assert routes._schedule_final_score_display(room.room_id) is None

        routes.room_manager.delete_room(room.room_id)

    asyncio.run(scenario())


def test_draw_waits_in_final_score_display_before_terminal_draw(monkeypatch):
    async def scenario():
        room = create_final_trick_room("final-draw", 4, draw=True)
        sockets = await connect_sockets(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.01)

        await routes._schedule_trick_resolution(room.room_id)
        state = room.engine.state
        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
        assert state.final_result == "DRAW"
        assert state.captured_mendis == {
            "TeamA": [Suit.SPADES],
            "TeamB": [Suit.HEARTS],
        }
        assert GamePhase.DRAW.value not in game_phases(sockets["P1"])

        await routes._final_score_display_tasks[room.room_id]
        assert state.phase == GamePhase.DRAW
        assert game_phases(sockets["P1"]).count(GamePhase.DRAW.value) == 1
        routes.room_manager.delete_room(room.room_id)

    asyncio.run(scenario())


def test_non_final_trick_still_returns_to_playing(monkeypatch):
    async def scenario():
        room = create_final_trick_room("non-final", 4)
        state = room.engine.state
        state.hands = {
            player.player_id: [Card(Suit.CLUBS, Rank.THREE)]
            for player in state.players
        }
        # The completed first trick already has no cards, so provide cards for
        # the next trick before resolving it; this must remain a non-final game.
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)

        await routes._schedule_trick_resolution(room.room_id)
        assert state.phase == GamePhase.PLAYING
        assert state.final_result is None
        assert state.current_turn == "P2"
        routes.room_manager.delete_room(room.room_id)

    asyncio.run(scenario())


def test_stale_final_timer_cannot_transition_a_newer_state(monkeypatch):
    async def scenario():
        room = create_final_trick_room("stale-final", 4)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.01)

        await routes._schedule_trick_resolution(room.room_id)
        state = room.engine.state
        task = routes._final_score_display_tasks[room.room_id]
        state.version += 1

        await task
        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
        assert room.room_id not in routes._final_score_display_tasks
        routes.room_manager.delete_room(room.room_id)

    asyncio.run(scenario())


def test_reconnect_during_final_score_display_returns_scoreboard_and_keeps_timer(
    monkeypatch,
):
    async def scenario():
        room = create_final_trick_room("reconnect-final", 4)
        sockets = await connect_sockets(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)
        monkeypatch.setattr(routes, "FINAL_SCORE_DISPLAY_DURATION_SECONDS", 0.01)

        await routes._schedule_trick_resolution(room.room_id)
        task = routes._final_score_display_tasks[room.room_id]
        replacement = Socket()
        await routes.connection_manager.connect(room.room_id, "P1", replacement)

        await routes._broadcast_game_states(room.room_id)
        snapshot = replacement.messages[-1]["payload"]
        assert snapshot["phase"] == GamePhase.FINAL_SCORE_DISPLAY.value
        assert snapshot["captured_mendis"] == {
            "TeamA": [],
            "TeamB": ["HEARTS"],
        }
        assert routes._schedule_final_score_display(room.room_id) is task

        await task
        assert room.engine.state.phase == GamePhase.GAME_OVER
        assert sockets["P1"].messages[-1]["payload"]["phase"] == (
            GamePhase.FINAL_SCORE_DISPLAY.value
        )
        assert replacement.messages[-1]["payload"]["phase"] == GamePhase.GAME_OVER.value
        routes.room_manager.delete_room(room.room_id)

    asyncio.run(scenario())