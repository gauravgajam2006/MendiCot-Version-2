import asyncio

import pytest

from mendicot.api import routes
from mendicot.enums import GamePhase, Rank, Suit
from mendicot.exceptions import InvalidPhase
from mendicot.models import Card, PlayedCard, Trick


def _create_started_room(room_id: str, player_count: int, cards_per_player: int = 2):
    room = routes.room_manager.create_room(room_id, player_count, "normal")
    for index in range(player_count):
        room.add_player(f"P{index + 1}", f"Player {index + 1}")
        room.set_player_online(f"P{index + 1}", True)
    room.start_game("P1", set(room.player_ids))
    room.select_first_player("P1", "P1")

    ranks = list(Rank)
    for index, player_id in enumerate(room.engine.state.seat_order):
        room.engine.state.hands[player_id] = [
            Card(Suit.HEARTS, ranks[index]),
            Card(Suit.CLUBS, ranks[index]),
        ][:cards_per_player]
    return room


def _complete_current_trick(room):
    state = room.engine.state
    for _ in range(state.player_count):
        player_id = state.current_turn
        room.engine.play_card(player_id, state.hands[player_id][0])
    return state


def _cleanup_room(room_id):
    if room_id in routes.room_manager._rooms:
        routes.room_manager.delete_room(room_id)


@pytest.mark.parametrize("player_count", [4, 6, 8])
def test_completed_trick_remains_public_for_configured_player_count(player_count):
    room_id = f"visible-{player_count}"
    try:
        room = _create_started_room(room_id, player_count)
        state = _complete_current_trick(room)
        winner_id = room.engine.get_current_trick_leader().player_id

        assert state.phase == GamePhase.TRICK_RESOLUTION
        assert state.current_turn is None
        assert len(state.current_trick.played_cards) == player_count
        assert state.current_trick.completed is True
        assert state.current_trick.winner_player_id == winner_id
        assert len(state.completed_tricks) == 0
        assert sum(team.tricks_won for team in state.teams.values()) == 0
        assert all(
            len(room.engine.get_player_view(player_id)["current_trick"]["played_cards"])
            == player_count
            for player_id in state.seat_order
        )
        assert all(
            room.engine.get_player_view(player_id)["current_trick_leader"]["player_id"]
            == winner_id
            for player_id in state.seat_order
        )
        assert all(
            room.engine.get_player_view(player_id)["current_trick"]["winner_player_id"]
            == winner_id
            for player_id in state.seat_order
        )

        with pytest.raises(InvalidPhase):
            room.engine.play_card("P1", state.hands["P1"][0])
    finally:
        _cleanup_room(room_id)


def test_delayed_resolution_clears_once_scores_once_and_sets_next_leader(monkeypatch):
    async def scenario():
        room_id = "resolve-once"
        room = _create_started_room(room_id, 4)
        state = _complete_current_trick(room)
        winner_id = room.engine.get_current_trick_leader().player_id
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.001)

        first_task = routes._schedule_trick_resolution(room_id)
        duplicate_task = routes._schedule_trick_resolution(room_id)
        assert duplicate_task is first_task
        await first_task

        assert len(state.completed_tricks) == 1
        assert state.current_trick.played_cards == []
        assert state.current_turn == winner_id
        assert state.phase == GamePhase.PLAYING
        assert sum(team.tricks_won for team in state.teams.values()) == 1
        assert sum(team.tens_captured for team in state.teams.values()) == 0

        await asyncio.sleep(0.003)
        assert len(state.completed_tricks) == 1
        assert sum(team.tricks_won for team in state.teams.values()) == 1
        _cleanup_room(room_id)

    asyncio.run(scenario())


def test_captured_ten_is_counted_exactly_once(monkeypatch):
    async def scenario():
        room_id = "ten-once"
        room = _create_started_room(room_id, 4)
        room.engine.state.hands["P1"][0] = Card(Suit.HEARTS, Rank.TEN)
        state = _complete_current_trick(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0)

        task = routes._schedule_trick_resolution(room_id)
        await task
        routes._schedule_trick_resolution(room_id)
        await asyncio.sleep(0)

        assert len(state.completed_tricks) == 1
        assert sum(team.tens_captured for team in state.teams.values()) == 1
        assert sum(team.tricks_won for team in state.teams.values()) == 1
        _cleanup_room(room_id)

    asyncio.run(scenario())


def test_stale_resolution_task_cannot_clear_a_newer_trick(monkeypatch):
    async def scenario():
        room_id = "stale-generation"
        room = _create_started_room(room_id, 4)
        state = _complete_current_trick(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.01)
        task = routes._schedule_trick_resolution(room_id)

        newer_trick = Trick(
            lead_player_id="P1",
            lead_suit=Suit.CLUBS,
            played_cards=[PlayedCard("P1", Card(Suit.CLUBS, Rank.ACE))],
        )
        state.current_trick = newer_trick
        state.version += 1
        room.trick_resolution_generation += 1

        await task
        assert state.current_trick is newer_trick
        assert len(state.completed_tricks) == 0
        _cleanup_room(room_id)

    asyncio.run(scenario())


def test_final_trick_is_displayed_before_game_over(monkeypatch):
    async def scenario():
        room_id = "final-visible"
        room = _create_started_room(room_id, 4, cards_per_player=1)
        state = _complete_current_trick(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 0.001)

        assert state.phase == GamePhase.TRICK_RESOLUTION
        assert len(state.current_trick.played_cards) == 4
        assert len(state.completed_tricks) == 0

        await routes._schedule_trick_resolution(room_id)

        assert state.phase == GamePhase.FINAL_SCORE_DISPLAY
        assert state.final_result in ("TeamA", "TeamB", "DRAW")
        assert state.current_trick.played_cards == []
        assert len(state.completed_tricks) == 1
        _cleanup_room(room_id)

    asyncio.run(scenario())


def test_room_deletion_cancels_pending_resolution(monkeypatch):
    async def scenario():
        room_id = "deleted-during-resolution"
        room = _create_started_room(room_id, 4)
        state = _complete_current_trick(room)
        monkeypatch.setattr(routes, "TRICK_DISPLAY_DURATION_SECONDS", 1)
        task = routes._schedule_trick_resolution(room_id)

        routes.room_manager.delete_room(room_id)
        await asyncio.sleep(0)

        assert task.done()
        assert room_id not in routes._trick_resolution_tasks
        assert len(state.completed_tricks) == 0
        assert len(state.current_trick.played_cards) == 4

    asyncio.run(scenario())
