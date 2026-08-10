import asyncio
import pytest

from mendicot.api import routes
from mendicot.enums import GamePhase
from mendicot.room import GameRoom, RoomStatus
from mendicot.turn_reminders import TurnReminderScheduler, TurnReminderState


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self._waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + delay, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        for _, future in [item for item in self._waiters if item[0] <= self.now]:
            if not future.done():
                future.set_result(None)
        self._waiters = [item for item in self._waiters if not item[1].done()]
        for _ in range(4):
            await asyncio.sleep(0)


def _state(identity=(1, 1), player="P1", targets=("target",)):
    return TurnReminderState(identity, player, targets)


def test_exact_initial_and_repeating_timing_and_no_duplicate_cycle():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        sends = []

        async def send(target):
            sends.append((clock.now, target))

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        first = scheduler.sync_room("room")
        await asyncio.sleep(0)
        assert scheduler.sync_room("room") is first
        assert sends == []
        await clock.advance(3.999)
        assert sends == []
        await clock.advance(0.001)
        assert sends == [(4.0, "target")]
        await clock.advance(9.999)
        assert len(sends) == 1
        await clock.advance(0.001)
        await clock.advance(10.0)
        assert [when for when, _ in sends] == [4.0, 14.0, 24.0]
        await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "replacement",
    [
        None,  # room deletion, no registration, removal, terminal phase
        _state(identity=(1, 2), player="P2"),  # turn change
        _state(identity=(2, 1)),  # match/engine/turn generation change
    ],
)
def test_stale_state_never_sends_at_timer_boundary(replacement):
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        sends = []

        async def send(target):
            sends.append(target)

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        current["room"] = replacement
        await clock.advance(4.0)
        assert sends == []
        assert scheduler.active_task("room") is None

    asyncio.run(scenario())


def test_card_play_after_first_send_cancels_repeat():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        sends = []

        async def send(target):
            sends.append(target)

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        assert len(sends) == 1
        current["room"] = _state(identity=(1, 2), player="P2")
        scheduler.sync_room("room")
        await clock.advance(10.0)
        assert len(sends) == 1
        await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("readiness_change", ["preference_on", "register", "token_change"])
def test_mid_turn_readiness_always_gets_fresh_four_second_grace(readiness_change):
    async def scenario():
        clock = FakeClock()
        current = {"room": _state(targets=())}
        sends = []

        async def send(target):
            sends.append((clock.now, target))

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await clock.advance(8.0)
        current["room"] = _state(targets=(f"{readiness_change}-target",))
        scheduler.sync_room("room", fresh_grace=True)
        await asyncio.sleep(0)
        await clock.advance(3.999)
        assert sends == []
        await clock.advance(0.001)
        assert sends == [(12.0, f"{readiness_change}-target")]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_rapid_off_on_invalidates_old_cycle_and_restarts_grace():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        sends = []

        async def send(target):
            sends.append(clock.now)

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(3.0)
        current["room"] = _state(targets=())
        scheduler.sync_room("room")
        current["room"] = _state(targets=("new-target",))
        scheduler.sync_room("room", fresh_grace=True)
        await asyncio.sleep(0)
        await clock.advance(3.999)
        assert sends == []
        await clock.advance(0.001)
        assert sends == [pytest.approx(7.0)]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_transient_sender_failure_keeps_repeat_cycle_alive():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        attempts = []

        async def send(target):
            attempts.append(clock.now)
            if len(attempts) == 1:
                raise RuntimeError("transient")

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        await clock.advance(10.0)
        assert attempts == [4.0, 14.0]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_turn_change_during_send_prevents_subsequent_repeats():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        started = asyncio.Event()
        release = asyncio.Event()
        attempts = []

        async def send(target):
            attempts.append(target)
            started.set()
            await release.wait()

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        await started.wait()
        current["room"] = _state(identity=(1, 2), player="P2")
        scheduler.sync_room("room")
        release.set()
        await clock.advance(20.0)
        assert attempts == ["target"]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_state_is_revalidated_before_each_registration_target():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state(targets=("first", "second"))}
        attempts = []

        async def send(target):
            attempts.append(target)
            current["room"] = _state(identity=(1, 2), player="P2")

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        assert attempts == ["first"]
        assert scheduler.active_task("room") is None

    asyncio.run(scenario())


@pytest.fixture
def clean_route_state():
    routes.turn_reminder_scheduler.cancel_all()
    routes.room_manager._rooms.clear()
    routes.session_tokens.clear()
    routes.push_registrations.clear()
    yield
    routes.turn_reminder_scheduler.cancel_all()
    routes.room_manager._rooms.clear()
    routes.session_tokens.clear()
    routes.push_registrations.clear()


@pytest.mark.parametrize("player_count", [4, 6, 8])
@pytest.mark.parametrize("trump_mode", ["normal", "hidden"])
def test_authoritative_readiness_is_player_count_and_mode_agnostic(
    clean_route_state, player_count, trump_mode
):
    room = GameRoom("ROOM", player_count, trump_mode)
    routes.room_manager._rooms[room.room_id] = room
    for index in range(player_count):
        room.add_player(f"P{index + 1}")
    room.start_game("P1", set(room.player_ids))
    if trump_mode == "hidden":
        room.select_trump_hider("P1", "P1")
    room.select_first_player("P1", "P1")
    if trump_mode == "hidden":
        room.engine.select_hidden_card("P1", 0)
        room.engine.complete_hidden_trump_setup("P1")
    room.begin_playable_turn()
    token = f"session-{player_count}-{trump_mode}"
    routes.session_tokens[token] = {"room_id": room.room_id, "player_id": "P1"}
    routes.push_registrations.register(room.room_id, "P1", token, "target", True)

    state = routes._get_turn_reminder_state(room.room_id)
    assert state is not None
    assert state.player_id == "P1"
    assert state.targets == ("target",)


@pytest.mark.parametrize(
    "phase",
    [phase for phase in GamePhase if phase != GamePhase.PLAYING],
)
def test_non_playable_phases_never_produce_delivery_state(clean_route_state, phase):
    room = GameRoom("ROOM", 4, "normal")
    routes.room_manager._rooms[room.room_id] = room
    for index in range(4):
        room.add_player(f"P{index + 1}")
    room.start_game("P1", set(room.player_ids))
    room.select_first_player("P1", "P1")
    room.engine.state.phase = phase
    token = "session"
    routes.session_tokens[token] = {"room_id": room.room_id, "player_id": "P1"}
    routes.push_registrations.register(room.room_id, "P1", token, "target", True)
    assert routes._get_turn_reminder_state(room.room_id) is None


def test_hidden_reveal_keeps_same_turn_generation(clean_route_state):
    room = GameRoom("ROOM", 4, "hidden")
    routes.room_manager._rooms[room.room_id] = room
    for index in range(4):
        room.add_player(f"P{index + 1}")
    room.start_game("P1", set(room.player_ids))
    room.select_trump_hider("P1", "P1")
    room.select_first_player("P1", "P1")
    room.engine.select_hidden_card("P1", 0)
    room.engine.complete_hidden_trump_setup("P1")
    room.begin_playable_turn()
    generation = room.turn_generation
    room.engine.state.phase = GamePhase.TRUMP_REVEAL_DISPLAY
    room.engine.state.phase = GamePhase.HIDDEN_CARD_RETURN
    room.engine.complete_hidden_card_return()
    assert room.turn_generation == generation
    assert room.engine.state.current_turn == "P1"


def _active_route_room(room_id="ROOM"):
    room = GameRoom(room_id, 4, "normal")
    routes.room_manager._rooms[room.room_id] = room
    for index in range(4):
        room.add_player(f"P{index + 1}")
    room.start_game("P1", set(room.player_ids))
    room.select_first_player("P1", "P1")
    room.begin_playable_turn()
    token = f"session-{room.room_id}"
    routes.session_tokens[token] = {"room_id": room.room_id, "player_id": "P1"}
    routes.push_registrations.register(room.room_id, "P1", token, "target", True)
    return room


def test_stale_registration_is_not_authorized_by_another_live_session(
    clean_route_state,
):
    room = _active_route_room()
    routes.push_registrations.remove_player(room.room_id, "P1")
    routes.push_registrations.register(
        room.room_id, "P1", "expired-session", "stale-target", True
    )

    assert routes._get_turn_reminder_state(room.room_id) is None


@pytest.mark.parametrize("leave_before_first", [False, True])
def test_authoritative_leave_stops_first_or_repeating_send(
    clean_route_state, monkeypatch, leave_before_first
):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []

        async def send(target):
            sends.append((clock.now, target))

        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state, send, sleep=clock.sleep
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        if not leave_before_first:
            await clock.advance(4.0)
            assert sends == [(4.0, "target")]

        # Explicit leave is valid once the match is terminal. The stale
        # PLAYING cycle must be invalidated by authoritative membership and
        # registration cleanup before another delivery boundary.
        room.engine.state.phase = GamePhase.GAME_OVER
        assert await routes._remove_lobby_player(room.room_id, "P1") is True
        await clock.advance(20.0)
        assert sends == ([] if leave_before_first else [(4.0, "target")])
        assert scheduler.active_task(room.room_id) is None
        await scheduler.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", [GamePhase.GAME_OVER, GamePhase.DRAW])
def test_terminal_phase_cancels_immediately(clean_route_state, monkeypatch, phase):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []

        async def send(target):
            sends.append(target)

        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state, send, sleep=clock.sleep
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        await clock.advance(4.0)
        room.engine.state.phase = phase
        routes._sync_turn_reminder(room.room_id)
        assert scheduler.active_task(room.room_id) is None
        await clock.advance(20.0)
        assert sends == ["target"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["room_deleted", "engine_cleared", "waiting"])
def test_room_lifecycle_invalidation_stops_cycle(
    clean_route_state, monkeypatch, mutation
):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []

        async def send(target):
            sends.append(target)

        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state, send, sleep=clock.sleep
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        await clock.advance(4.0)

        if mutation == "room_deleted":
            routes.room_manager.delete_room(room.room_id)
        elif mutation == "engine_cleared":
            room.engine = None
            routes._sync_turn_reminder(room.room_id)
        else:
            room._status = RoomStatus.WAITING
            routes._sync_turn_reminder(room.room_id)

        assert scheduler.active_task(room.room_id) is None
        await clock.advance(20.0)
        assert sends == ["target"]

    asyncio.run(scenario())


def test_return_to_lobby_final_reset_stops_cycle(clean_route_state, monkeypatch):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []

        async def send(target):
            sends.append(target)

        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state, send, sleep=clock.sleep
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        await clock.advance(4.0)
        room.engine.state.phase = GamePhase.GAME_OVER
        for player_id in room.player_ids:
            was_reset = room.return_to_lobby(player_id)
        assert was_reset is True
        routes._cancel_room_lifecycle(room.room_id)
        assert room.status == RoomStatus.WAITING
        assert room.engine is None
        await clock.advance(20.0)
        assert sends == ["target"]

    asyncio.run(scenario())


class _Socket:
    async def accept(self):
        pass

    async def close(self, **_kwargs):
        pass

    async def send_json(self, _message):
        pass


def test_temporary_disconnect_keeps_reminder_during_grace(
    clean_route_state, monkeypatch
):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []
        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state,
            lambda target: _record_send(sends, target),
            sleep=clock.sleep,
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 60)
        socket = _Socket()
        await routes.connection_manager.connect(room.room_id, "P1", socket)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        await routes._handle_socket_disconnect(room.room_id, "P1", socket)
        await clock.advance(4.0)
        assert sends == ["target"]
        assert routes.push_registrations.enabled_targets(room.room_id, "P1") == [
            "target"
        ]
        routes._cancel_disconnect_cleanup(room.room_id, "P1")
        await scheduler.shutdown()

    asyncio.run(scenario())


async def _record_send(sends, target):
    sends.append(target)


def test_permanent_disconnect_expiry_stops_active_game_reminder(
    clean_route_state, monkeypatch
):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []
        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state,
            lambda target: _record_send(sends, target),
            sleep=clock.sleep,
        )
        monkeypatch.setattr(routes, "turn_reminder_scheduler", scheduler)
        monkeypatch.setattr(routes, "DISCONNECTED_PLAYER_GRACE_PERIOD_SECONDS", 0)
        socket = _Socket()
        await routes.connection_manager.connect(room.room_id, "P1", socket)
        routes._sync_turn_reminder(room.room_id)
        await asyncio.sleep(0)
        await routes._handle_socket_disconnect(room.room_id, "P1", socket)
        for _ in range(8):
            await asyncio.sleep(0)
        assert room.player_ids[0] == "P1"  # gameplay state remains preserved
        assert routes.push_registrations.for_player(room.room_id, "P1") == []
        assert scheduler.active_task(room.room_id) is None
        await clock.advance(20.0)
        assert sends == []

    asyncio.run(scenario())


def test_old_cycle_cannot_survive_new_match_generation(clean_route_state):
    async def scenario():
        room = _active_route_room()
        clock = FakeClock()
        sends = []
        scheduler = TurnReminderScheduler(
            routes._get_turn_reminder_state,
            lambda target: _record_send(sends, target),
            sleep=clock.sleep,
        )
        scheduler.sync_room(room.room_id)
        await asyncio.sleep(0)
        await clock.advance(4.0)
        room.match_generation += 1
        scheduler.sync_room(room.room_id)
        await asyncio.sleep(0)
        await clock.advance(4.0)
        assert sends == ["target", "target"]
        await clock.advance(6.0)
        assert sends == ["target", "target"]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_cancelled_old_task_cleanup_cannot_remove_newer_task():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(_target):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        old_task = scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        await started.wait()
        current["room"] = _state(identity=(1, 2), player="P2")
        new_task = scheduler.sync_room("room")
        assert new_task is not old_task
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert scheduler.active_task("room") is new_task
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_leave_while_send_in_progress_allows_no_later_repeat():
    async def scenario():
        clock = FakeClock()
        current = {"room": _state()}
        started = asyncio.Event()
        completed = []

        async def send(target):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                completed.append(target)

        scheduler = TurnReminderScheduler(current.get, send, sleep=clock.sleep)
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(4.0)
        await started.wait()
        current["room"] = None
        scheduler.sync_room("room")
        await asyncio.sleep(0)
        await clock.advance(30.0)
        assert completed == ["target"]
        assert scheduler.active_task("room") is None

    asyncio.run(scenario())
