"""Authoritative, generation-guarded background turn reminders."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Hashable

logger = logging.getLogger(__name__)

INITIAL_TURN_ALERT_DELAY_SECONDS = 4.0
TURN_ALERT_REPEAT_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class TurnReminderState:
    """Immutable authoritative snapshot used to validate one playable turn."""

    identity: Hashable
    player_id: str
    targets: tuple[str, ...]


StateProvider = Callable[[str], TurnReminderState | None]
Sender = Callable[[str], Awaitable[object]]
Sleeper = Callable[[float], Awaitable[None]]


class TurnReminderScheduler:
    """Own at most one reminder cycle for each room."""

    def __init__(
        self,
        state_provider: StateProvider,
        sender: Sender,
        *,
        sleep: Sleeper = asyncio.sleep,
        initial_delay: float = INITIAL_TURN_ALERT_DELAY_SECONDS,
        repeat_interval: float = TURN_ALERT_REPEAT_INTERVAL_SECONDS,
    ) -> None:
        self._state_provider = state_provider
        self._sender = sender
        self._sleep = sleep
        self._initial_delay = initial_delay
        self._repeat_interval = repeat_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._identities: dict[str, Hashable] = {}
        self._cycle_generations: dict[str, int] = {}

    def sync_room(
        self, room_id: str, *, fresh_grace: bool = False
    ) -> asyncio.Task[None] | None:
        """Start, retain, replace, or cancel a cycle from current state."""
        state = self._state_provider(room_id)
        if state is None or not state.targets:
            self.cancel_room(room_id)
            return None

        existing = self._tasks.get(room_id)
        if (
            not fresh_grace
            and existing is not None
            and not existing.done()
            and self._identities.get(room_id) == state.identity
        ):
            return existing

        self.cancel_room(room_id)
        generation = self._cycle_generations.get(room_id, 0) + 1
        self._cycle_generations[room_id] = generation
        self._identities[room_id] = state.identity
        task = asyncio.create_task(
            self._run_cycle(room_id, state.identity, generation),
            name=f"turn-reminder:{room_id}:{generation}",
        )
        self._tasks[room_id] = task
        logger.debug(
            "turn reminder cycle started",
            extra={"room_id": room_id, "player_id": state.player_id},
        )
        return task

    def cancel_room(self, room_id: str) -> None:
        """Invalidate and idempotently cancel a room's current cycle."""
        self._cycle_generations[room_id] = self._cycle_generations.get(room_id, 0) + 1
        task = self._tasks.pop(room_id, None)
        self._identities.pop(room_id, None)
        if task is not None and not task.done():
            task.cancel()

    def cancel_player(self, room_id: str, player_id: str) -> None:
        state = self._state_provider(room_id)
        if state is None or state.player_id == player_id:
            self.cancel_room(room_id)

    def cancel_all(self) -> list[asyncio.Task[None]]:
        tasks = list(self._tasks.values())
        for room_id in list(self._tasks):
            self.cancel_room(room_id)
        return tasks

    async def shutdown(self) -> None:
        tasks = self.cancel_all()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def active_task(self, room_id: str) -> asyncio.Task[None] | None:
        return self._tasks.get(room_id)

    def _is_current(self, room_id: str, identity: Hashable, generation: int) -> bool:
        return (
            self._cycle_generations.get(room_id) == generation
            and self._identities.get(room_id) == identity
            and self._tasks.get(room_id) is asyncio.current_task()
        )

    def _validated_state(
        self, room_id: str, identity: Hashable, generation: int
    ) -> TurnReminderState | None:
        if not self._is_current(room_id, identity, generation):
            return None
        state = self._state_provider(room_id)
        if state is None or state.identity != identity or not state.targets:
            return None
        return state

    async def _run_cycle(
        self, room_id: str, identity: Hashable, generation: int
    ) -> None:
        try:
            await self._sleep(self._initial_delay)
            while True:
                state = self._validated_state(room_id, identity, generation)
                if state is None:
                    return
                for target in state.targets:
                    # Re-read state for every individual FCM send. Cancellation
                    # remains the prompt path; this guard is the race backstop.
                    current = self._validated_state(room_id, identity, generation)
                    if current is None:
                        return
                    if target not in current.targets:
                        continue
                    logger.debug(
                        "turn reminder delivery attempted",
                        extra={"room_id": room_id, "player_id": state.player_id},
                    )
                    try:
                        await self._sender(target)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning(
                            "turn reminder delivery failed",
                            extra={"room_id": room_id, "player_id": state.player_id},
                        )
                if self._validated_state(room_id, identity, generation) is None:
                    return
                await self._sleep(self._repeat_interval)
        except asyncio.CancelledError:
            return
        finally:
            if self._tasks.get(room_id) is asyncio.current_task():
                self._tasks.pop(room_id, None)
                self._identities.pop(room_id, None)
