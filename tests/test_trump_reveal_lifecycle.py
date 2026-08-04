"""Tests for the two-stage Hidden Trump reveal lifecycle.

Phase flow: PLAYING → TRUMP_REVEAL_DISPLAY → HIDDEN_CARD_RETURN → PLAYING

Engine-level tests are synchronous; route-level timer tests use asyncio.run.
"""

import asyncio

import pytest

from mendicot.api import routes
from mendicot.enums import GamePhase, Rank, Suit, TrumpStatus
from mendicot.exceptions import (
    InvalidPhase,
    InvalidTrumpAction,
    TrumpAlreadyRevealed,
)
from mendicot.models import Card, PlayedCard, Trick


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_HAND_SIZES = {4: 12, 6: 8, 8: 6}


def _setup_hidden_game(engine, players, hider_id="P1", card_position=0):
    """Create a hidden-trump game, select hider, select hidden card, complete setup."""
    engine.create_game(
        "reveal-test",
        players,
        host_id=players[0].player_id,
        hidden_trump_mode=True,
    )
    engine.select_trump_hider(hider_id, hider_id)
    engine.deal_cards()
    engine.select_hidden_card(hider_id, card_position)
    engine.complete_hidden_trump_setup(hider_id)
    return engine.state


def _setup_reveal_scenario(engine, players, hider_id="P1"):
    """Setup a hidden game and prepare hands so P3 is on cut for reveal.

    Returns (state, reveal_player_id).
    """
    state = _setup_hidden_game(engine, players, hider_id)
    hidden_card = engine._hidden_card

    # We want P3 to be on cut. If P3 has cards of the lead suit, swap them with P4.
    lead_suit = state.hands["P2"][0].suit
    for i, c3 in enumerate(state.hands["P3"]):
        if c3.suit == lead_suit:
            for j, c4 in enumerate(state.hands["P4"]):
                if c4.suit != lead_suit:
                    state.hands["P3"][i], state.hands["P4"][j] = state.hands["P4"][j], state.hands["P3"][i]
                    break

    # P2 leads
    engine.play_card("P2", state.hands["P2"][0])
    return state, "P3"


# ---------------------------------------------------------------------------
# 1. Physical card removal at selection
# ---------------------------------------------------------------------------

class TestPhysicalCardRemoval:

    @pytest.mark.parametrize("player_count,fixture", [
        (4, "four_players"),
        (6, "six_players"),
        (8, "eight_players"),
    ])
    def test_hidden_card_removed_from_hand(
        self, engine, player_count, fixture, request
    ):
        players = request.getfixturevalue(fixture)
        engine.create_game(
            "removal-test",
            players,
            host_id=players[0].player_id,
            hidden_trump_mode=True,
        )
        engine.select_trump_hider("P1", "P1")
        engine.deal_cards()

        full_hand_size = EXPECTED_HAND_SIZES[player_count]
        assert len(engine.state.hands["P1"]) == full_hand_size

        engine.select_hidden_card("P1", 0)

        # Hand shrinks by exactly 1 after selection.
        assert len(engine.state.hands["P1"]) == full_hand_size - 1
        # Card is in private engine storage.
        assert engine._hidden_card is not None
        assert engine._hidden_card_owner_id == "P1"
        assert engine._hidden_card_returned is False

    @pytest.mark.parametrize("player_count,fixture", [
        (4, "four_players"),
        (6, "six_players"),
        (8, "eight_players"),
    ])
    def test_total_ownership_48_after_selection(
        self, engine, player_count, fixture, request
    ):
        players = request.getfixturevalue(fixture)
        engine.create_game(
            "ownership-test",
            players,
            host_id=players[0].player_id,
            hidden_trump_mode=True,
        )
        engine.select_trump_hider("P1", "P1")
        engine.deal_cards()
        engine.select_hidden_card("P1", 0)

        visible_count = sum(
            len(hand) for hand in engine.state.hands.values()
        )
        hidden_count = 1 if engine._hidden_card is not None else 0
        assert visible_count + hidden_count == 48

    def test_hidden_card_not_in_any_hand(self, engine, four_players):
        engine.create_game(
            "not-in-hand",
            four_players,
            host_id="P1",
            hidden_trump_mode=True,
        )
        engine.select_trump_hider("P1", "P1")
        engine.deal_cards()
        engine.select_hidden_card("P1", 0)
        engine.complete_hidden_trump_setup("P1")

        hidden_card = engine._hidden_card
        for player_id, hand in engine.state.hands.items():
            assert hidden_card not in hand, (
                f"Hidden card found in {player_id}'s hand before reveal"
            )


# ---------------------------------------------------------------------------
# 2. REVEAL_TRUMP enters TRUMP_REVEAL_DISPLAY
# ---------------------------------------------------------------------------

class TestRevealEntersDisplayPhase:

    def test_valid_reveal_enters_display_phase(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY

    def test_trump_suit_becomes_public_immediately(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        assert state.trump_state.status == TrumpStatus.PUBLIC
        assert state.trump_state.suit is not None

    def test_hidden_card_not_yet_returned_during_display(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        assert engine._hidden_card_returned is False
        assert engine._hidden_card is not None

    def test_current_turn_preserved_during_reveal(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        turn_before = state.current_turn
        engine.reveal_trump(revealer)
        assert state.current_turn == turn_before

    def test_current_trick_preserved_during_reveal(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        trick_cards_before = len(state.current_trick.played_cards)
        lead_suit_before = state.current_trick.lead_suit
        engine.reveal_trump(revealer)
        assert len(state.current_trick.played_cards) == trick_cards_before
        assert state.current_trick.lead_suit == lead_suit_before

    def test_reveal_generation_incremented(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        gen_before = state.reveal_generation
        engine.reveal_trump(revealer)
        assert state.reveal_generation == gen_before + 1


# ---------------------------------------------------------------------------
# 3. Trump reveal display snapshot
# ---------------------------------------------------------------------------

class TestRevealDisplaySnapshot:

    def test_player_view_has_reveal_metadata(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        view = engine.get_player_view("P2")
        assert view["phase"] == GamePhase.TRUMP_REVEAL_DISPLAY.value
        assert view["trump_state"]["status"] == TrumpStatus.PUBLIC.value
        assert view["trump_state"]["suit"] is not None
        assert view["trump_reveal_display"] is not None
        assert view["trump_reveal_display"]["trump_hider_id"] == "P1"
        assert view["trump_reveal_display"]["reveal_actor_id"] == revealer

    def test_hidden_rank_not_exposed(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        for pid in state.seat_order:
            view = engine.get_player_view(pid)
            assert view["trump_state"]["hidden_rank"] is None
            assert view["trump_state"]["hidden_card_index"] is None

    def test_hider_hand_does_not_contain_hidden_card(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        hider_view = engine.get_player_view("P1")
        # Card count is hand size, hidden card not present.
        hand_in_view = hider_view["hands"]["P1"]
        hidden_card_dict = {
            "suit": engine._hidden_card.suit.value,
            "rank": engine._hidden_card.rank.value,
        }
        card_dicts = [
            {"suit": c["suit"], "rank": c["rank"]} for c in hand_in_view
        ]
        assert hidden_card_dict not in card_dicts


# ---------------------------------------------------------------------------
# 4. complete_trump_reveal_display → HIDDEN_CARD_RETURN
# ---------------------------------------------------------------------------

class TestCompleteTrumpRevealDisplay:

    def test_card_returns_to_hider(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        hidden_card = engine._hidden_card
        engine.complete_trump_reveal_display()

        assert state.phase == GamePhase.HIDDEN_CARD_RETURN
        assert engine._hidden_card is None
        assert engine._hidden_card_returned is True
        assert hidden_card in state.hands["P1"]

    def test_card_does_not_return_to_reveal_actor(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        assert revealer != "P1"  # revealer is P3, hider is P1
        engine.reveal_trump(revealer)
        hidden_card = engine._hidden_card
        engine.complete_trump_reveal_display()

        # Card must be in hider's (P1) hand, not revealer's (P3)
        assert hidden_card in state.hands["P1"]
        assert hidden_card not in state.hands[revealer]

    def test_card_appears_exactly_once(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        all_cards = [
            card
            for hand in state.hands.values()
            for card in hand
        ]
        hidden_card = next(
            c for c in state.hands["P1"]
            if c.suit == state.trump_state.suit
            and c.rank == state.trump_state.hidden_rank
        )
        assert all_cards.count(hidden_card) == 1

    def test_total_ownership_48_after_return(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        total = sum(len(hand) for hand in state.hands.values())
        total += len(state.current_trick.played_cards)
        hidden = 1 if engine._hidden_card is not None else 0
        assert total + hidden == 48

    def test_duplicate_return_impossible(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        hider_hand_size = len(state.hands["P1"])

        # Trying to complete reveal display again raises InvalidPhase
        # because phase is now HIDDEN_CARD_RETURN.
        with pytest.raises(InvalidPhase):
            engine.complete_trump_reveal_display()

        # Hand size unchanged.
        assert len(state.hands["P1"]) == hider_hand_size

    def test_current_turn_preserved(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        turn_before = state.current_turn
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        assert state.current_turn == turn_before

    def test_current_trick_preserved(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        trick_cards = len(state.current_trick.played_cards)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        assert len(state.current_trick.played_cards) == trick_cards


# ---------------------------------------------------------------------------
# 5. HIDDEN_CARD_RETURN phase and views
# ---------------------------------------------------------------------------

class TestHiddenCardReturnPhase:

    def test_hidden_card_return_broadcast_metadata(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        view = engine.get_player_view("P2")
        assert view["phase"] == GamePhase.HIDDEN_CARD_RETURN.value
        assert view["hidden_card_return"] is not None
        assert view["hidden_card_return"]["hider_id"] == "P1"
        assert view["hidden_card_return"]["returned"] is True

    def test_hider_private_view_contains_returned_card(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        hidden_card = engine._hidden_card
        engine.complete_trump_reveal_display()

        hider_view = engine.get_player_view("P1")
        hand_cards = hider_view["hands"]["P1"]
        card_tuples = [(c["suit"], c["rank"]) for c in hand_cards]
        assert (hidden_card.suit.value, hidden_card.rank.value) in card_tuples

    def test_non_hider_does_not_see_card_identity(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        for pid in state.seat_order:
            view = engine.get_player_view(pid)
            assert view["trump_state"]["hidden_rank"] is None
            assert view["trump_state"]["hidden_card_index"] is None

    def test_play_card_disabled_during_return(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        assert state.phase == GamePhase.HIDDEN_CARD_RETURN

        with pytest.raises(InvalidPhase):
            engine.play_card("P3", state.hands["P3"][0])

    def test_duplicate_reveal_rejected_during_return(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        with pytest.raises(InvalidPhase):
            engine.reveal_trump(revealer)


# ---------------------------------------------------------------------------
# 6. complete_hidden_card_return → PLAYING
# ---------------------------------------------------------------------------

class TestCompleteHiddenCardReturn:

    def test_phase_becomes_playing(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        engine.complete_hidden_card_return()
        assert state.phase == GamePhase.PLAYING

    def test_current_turn_preserved_after_full_lifecycle(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        turn_before = state.current_turn
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        engine.complete_hidden_card_return()
        assert state.current_turn == turn_before

    def test_current_trick_preserved_after_full_lifecycle(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        trick_cards = len(state.current_trick.played_cards)
        lead_suit = state.current_trick.lead_suit
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        engine.complete_hidden_card_return()
        assert len(state.current_trick.played_cards) == trick_cards
        assert state.current_trick.lead_suit == lead_suit

    def test_player_can_play_card_after_return(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()
        engine.complete_hidden_card_return()
        assert state.phase == GamePhase.PLAYING
        assert state.current_turn == revealer
        # Player should be able to play a card now
        engine.play_card(revealer, state.hands[revealer][0])


# ---------------------------------------------------------------------------
# 7. Rejection and edge cases
# ---------------------------------------------------------------------------

class TestRevealRejection:

    def test_play_card_rejected_during_display(self, engine, four_players):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY
        with pytest.raises(InvalidPhase):
            engine.play_card(revealer, state.hands[revealer][0])

    def test_duplicate_reveal_rejected_during_display(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        with pytest.raises(InvalidPhase):
            engine.reveal_trump(revealer)

    def test_reveal_during_trick_resolution_rejected(
        self, engine, four_players
    ):
        state = _setup_hidden_game(engine, four_players)
        # Play a full trick to reach TRICK_RESOLUTION
        for i, pid in enumerate(state.seat_order):
            state.hands[pid] = [
                Card(Suit.HEARTS, Rank.THREE + i),
                Card(Suit.CLUBS, Rank.THREE + i),
            ]
        for _ in range(state.player_count):
            pid = state.current_turn
            engine.play_card(pid, state.hands[pid][0])

        assert state.phase == GamePhase.TRICK_RESOLUTION
        with pytest.raises(InvalidPhase):
            engine.reveal_trump("P1")

    def test_normal_mode_never_uses_reveal_phases(
        self, engine, four_players
    ):
        engine.create_game(
            "normal-game",
            four_players,
            host_id="P1",
            hidden_trump_mode=False,
        )
        engine.deal_cards()

        assert engine.state.phase == GamePhase.PLAYING
        assert engine.state.trump_state.status == TrumpStatus.NONE

        # Normal trump mode: reveal_trump should fail because status is NONE
        # (which isn't HIDDEN).
        state = engine.state
        state.hands["P1"] = [Card(Suit.HEARTS, r) for r in range(3, 15)]
        state.hands["P2"] = [Card(Suit.SPADES, r) for r in range(3, 15)]
        engine.play_card("P1", state.hands["P1"][0])

        with pytest.raises(TrumpAlreadyRevealed):
            engine.reveal_trump("P2")


# ---------------------------------------------------------------------------
# 8. Multi-player modes
# ---------------------------------------------------------------------------

class TestMultiPlayerReveal:

    @pytest.mark.parametrize("fixture", [
        "four_players", "six_players", "eight_players",
    ])
    def test_full_reveal_lifecycle(self, engine, fixture, request):
        players = request.getfixturevalue(fixture)
        player_count = len(players)

        state = _setup_hidden_game(engine, players)
        expected_hand_after_return = EXPECTED_HAND_SIZES[player_count]

        # Make P3 on cut by swapping lead suit cards with P4.
        lead_suit = state.hands["P2"][0].suit
        for i, c3 in enumerate(state.hands["P3"]):
            if c3.suit == lead_suit:
                for j, c4 in enumerate(state.hands["P4"]):
                    if c4.suit != lead_suit:
                        state.hands["P3"][i], state.hands["P4"][j] = state.hands["P4"][j], state.hands["P3"][i]
                        break

        engine.play_card("P2", state.hands["P2"][0])

        turn_before = state.current_turn
        trick_cards = len(state.current_trick.played_cards)

        engine.reveal_trump("P3")
        assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY

        engine.complete_trump_reveal_display()
        assert state.phase == GamePhase.HIDDEN_CARD_RETURN
        assert len(state.hands["P1"]) == expected_hand_after_return

        engine.complete_hidden_card_return()
        assert state.phase == GamePhase.PLAYING
        assert state.current_turn == turn_before
        assert len(state.current_trick.played_cards) == trick_cards

        # Verify total ownership
        total = sum(len(hand) for hand in state.hands.values())
        total += len(state.current_trick.played_cards)
        hidden = 1 if engine._hidden_card is not None else 0
        assert total + hidden == 48


# ---------------------------------------------------------------------------
# 9. Route-level timer tests (async)
# ---------------------------------------------------------------------------

def _create_hidden_started_room(room_id, player_count):
    """Create a hidden-trump room that is fully started with hands dealt."""
    room = routes.room_manager.create_room(room_id, player_count, "hidden")
    for i in range(player_count):
        room.add_player(f"P{i + 1}", f"Player {i + 1}")
        room.set_player_online(f"P{i + 1}", True)
    room.start_game("P1", set(room.player_ids))
    room.engine.select_trump_hider("P1", "P1")
    room.select_first_player("P1", "P2")
    return room


def _setup_room_reveal(room, player_count):
    """Setup hands for reveal and trigger REVEAL_TRUMP."""
    state = room.engine.state
    hand_size = EXPECTED_HAND_SIZES[player_count]

    # Ensure hidden card is selected and setup is complete
    # (select_first_player already dealt + selected)
    room.engine.select_hidden_card("P1", 0)
    room.engine.complete_hidden_trump_setup("P1")

    state.hands["P2"] = [
        Card(Suit.HEARTS, rank) for rank in range(3, 15)
    ][:hand_size]
    state.hands["P3"] = [
        Card(Suit.SPADES, rank) for rank in range(3, 15)
    ][:hand_size]

    room.engine.play_card("P2", state.hands["P2"][0])
    room.engine.reveal_trump("P3")
    return state


def _cleanup_room(room_id):
    if room_id in routes.room_manager._rooms:
        routes.room_manager.delete_room(room_id)


class TestRouteTimerLifecycle:

    def test_timer_driven_full_lifecycle(self, monkeypatch):
        async def scenario():
            room_id = "reveal-timer"
            try:
                room = _create_hidden_started_room(room_id, 4)
                state = _setup_room_reveal(room, 4)
                monkeypatch.setattr(
                    routes, "TRUMP_REVEAL_DISPLAY_DURATION_SECONDS", 0.001
                )
                monkeypatch.setattr(
                    routes, "HIDDEN_CARD_RETURN_DURATION_SECONDS", 0.001
                )

                assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY
                turn_before = state.current_turn
                trick_count = len(state.current_trick.played_cards)

                task1 = routes._schedule_trump_reveal_display(room_id)
                await task1

                assert state.phase == GamePhase.HIDDEN_CARD_RETURN

                task2 = routes._schedule_hidden_card_return(room_id)
                # task2 may have been created by the first timer
                if task2 is None:
                    task2 = routes._hidden_card_return_tasks.get(room_id)
                if task2:
                    await task2

                assert state.phase == GamePhase.PLAYING
                assert state.current_turn == turn_before
                assert len(state.current_trick.played_cards) == trick_count
            finally:
                _cleanup_room(room_id)

        asyncio.run(scenario())

    def test_duplicate_timer_returns_same_task(self, monkeypatch):
        async def scenario():
            room_id = "dup-timer"
            try:
                room = _create_hidden_started_room(room_id, 4)
                _setup_room_reveal(room, 4)
                monkeypatch.setattr(
                    routes, "TRUMP_REVEAL_DISPLAY_DURATION_SECONDS", 1.0
                )

                task1 = routes._schedule_trump_reveal_display(room_id)
                task2 = routes._schedule_trump_reveal_display(room_id)
                assert task2 is task1

                task1.cancel()
                await asyncio.sleep(0)
            finally:
                _cleanup_room(room_id)

        asyncio.run(scenario())

    def test_room_deletion_cancels_reveal_timers(self, monkeypatch):
        async def scenario():
            room_id = "delete-reveal"
            room = _create_hidden_started_room(room_id, 4)
            _setup_room_reveal(room, 4)
            monkeypatch.setattr(
                routes, "TRUMP_REVEAL_DISPLAY_DURATION_SECONDS", 1.0
            )

            task = routes._schedule_trump_reveal_display(room_id)
            routes.room_manager.delete_room(room_id)
            await asyncio.sleep(0)

            assert task.done()
            assert room_id not in routes._trump_reveal_display_tasks

        asyncio.run(scenario())

    def test_stale_timer_ignored(self, monkeypatch):
        async def scenario():
            room_id = "stale-reveal"
            try:
                room = _create_hidden_started_room(room_id, 4)
                state = _setup_room_reveal(room, 4)
                monkeypatch.setattr(
                    routes, "TRUMP_REVEAL_DISPLAY_DURATION_SECONDS", 0.01
                )

                task = routes._schedule_trump_reveal_display(room_id)

                # Artificially bump generation to make timer stale
                room.trump_reveal_generation += 1

                await task
                # Phase should NOT have changed because the timer was stale
                assert state.phase == GamePhase.TRUMP_REVEAL_DISPLAY
            finally:
                _cleanup_room(room_id)

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 10. Reconnect during reveal phases
# ---------------------------------------------------------------------------

class TestReconnectDuringReveal:

    def test_reconnect_during_reveal_display_gets_state(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)

        # Simulate reconnect: player gets correct view
        for pid in state.seat_order:
            view = engine.get_player_view(pid)
            assert view["phase"] == GamePhase.TRUMP_REVEAL_DISPLAY.value
            assert view["trump_state"]["status"] == TrumpStatus.PUBLIC.value
            assert view["trump_state"]["suit"] is not None
            assert view["trump_reveal_display"] is not None

    def test_reconnect_during_hidden_card_return_hider_gets_card(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        hidden_card = engine._hidden_card
        engine.complete_trump_reveal_display()

        # Hider reconnects: should see the returned card
        hider_view = engine.get_player_view("P1")
        assert hider_view["phase"] == GamePhase.HIDDEN_CARD_RETURN.value
        hand_cards = hider_view["hands"]["P1"]
        card_tuples = [(c["suit"], c["rank"]) for c in hand_cards]
        assert (hidden_card.suit.value, hidden_card.rank.value) in card_tuples

    def test_reconnect_during_return_non_hider_no_card(
        self, engine, four_players
    ):
        state, revealer = _setup_reveal_scenario(engine, four_players)
        engine.reveal_trump(revealer)
        engine.complete_trump_reveal_display()

        # Non-hider should not see hidden card details
        view = engine.get_player_view("P2")
        assert view["trump_state"]["hidden_rank"] is None
        assert view["trump_state"]["hidden_card_index"] is None
        assert view["hidden_card_return"]["returned"] is True


# ---------------------------------------------------------------------------
# 11. Auto-reveal removed from play_card
# ---------------------------------------------------------------------------

class TestAutoRevealRemoved:

    def test_on_cut_with_trump_does_not_auto_reveal(
        self, engine, four_players
    ):
        """Playing a card on cut with trump cards must NOT auto-reveal.

        The player must use REVEAL_TRUMP explicitly.
        """
        state = _setup_hidden_game(engine, four_players)
        trump_suit = state.trump_state.suit

        # P2 leads with HEARTS
        state.hands["P2"] = [
            Card(Suit.HEARTS, rank) for rank in range(3, 15)
        ]
        # P3 has trump cards but no HEARTS
        state.hands["P3"] = [
            Card(trump_suit, rank) for rank in range(3, 15)
        ]

        engine.play_card("P2", state.hands["P2"][0])

        # P3 plays trump on cut — should NOT auto-reveal.
        # With auto-reveal removed, playing trump on cut while
        # status is HIDDEN means validate_follow_suit won't enforce
        # MustPlayTrump (since status isn't PUBLIC), so any card is allowed.
        engine.play_card("P3", state.hands["P3"][0])
        assert state.trump_state.status == TrumpStatus.HIDDEN
