"""Atomic, monotonic in-process fleet-tick replay controller."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

from .async_utils import finish_before_cancellation
from .async_utils import run_sync as run_in_threadpool
from .store import FleetStore, load_events_jsonl, parse_timestamp

LOGGER = logging.getLogger(__name__)
ResultT = TypeVar("ResultT")


class ReplayOperationError(RuntimeError):
    """A synchronous replay action failed and is safe to retry."""


class ReplayRevisionConflict(RuntimeError):
    """The caller attempted a mutation against an old replay revision."""

    def __init__(self, current_revision: int):
        self.current_revision = current_revision
        super().__init__("replay revision does not match current state")


class ReplayController:
    def __init__(
        self,
        store: FleetStore,
        telemetry_path: str | Path,
        *,
        default_ticks_per_second: float = 2.0,
        loop: bool = False,
    ):
        self.store = store
        self.telemetry_path = Path(telemetry_path)
        self.events = self._load_events()
        self.ticks = self._group_ticks(self.events)
        self.current = self._initial_cursor()
        self.ticks_per_second = default_ticks_per_second
        self.state = "complete" if self.current >= len(self.ticks) else "paused"
        self.last_error: str | None = None
        self.revision = store.replay_control_revision()
        self.loop = loop
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def _load_events(self) -> list[dict[str, Any]]:
        events = load_events_jsonl(self.telemetry_path)
        events.sort(key=lambda item: parse_timestamp(item["timestamp"])[1])
        return events

    @staticmethod
    def _group_ticks(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        return [
            list(group)
            for _, group in itertools.groupby(
                events, key=lambda item: parse_timestamp(item["timestamp"])[1]
            )
        ]

    def _initial_cursor(self) -> int:
        cursor = 0
        for tick in self.ticks:
            if not all(self.store.event_status(event) == "exact" for event in tick):
                break
            cursor += 1
        return cursor

    def seed_to_checkpoint(self, initial_tick: int) -> None:
        """Synchronously seed through a zero-based initial tick at startup."""

        if initial_tick < 0:
            raise ValueError("initial checkpoint must be zero or greater")
        if initial_tick >= len(self.ticks):
            raise ValueError("initial checkpoint is outside the telemetry fixture")
        target = initial_tick + 1
        if self.current > target:
            raise ValueError("persisted replay state is beyond the initial checkpoint")
        for tick in self.ticks[self.current : target]:
            self.store.ingest_events(tick, trusted=True)
            self.current += 1
        if self.current != target:
            raise RuntimeError("replay did not reach the exact initial checkpoint")
        self.state = "complete" if self.current >= len(self.ticks) else "paused"

    @staticmethod
    async def _finish_before_cancellation(operation: Awaitable[ResultT]) -> ResultT:
        """Let one logical store/state commit settle before cancellation escapes."""

        return await finish_before_cancellation(operation)

    async def _touch(self) -> None:
        async def persist() -> None:
            try:
                self.revision = await run_in_threadpool(
                    self.store.next_replay_control_revision,
                    self.revision,
                )
            except Exception as exc:
                LOGGER.exception("Replay control revision could not be persisted")
                raise ReplayOperationError(
                    "Replay control state could not be persisted. Retry after storage recovers."
                ) from exc

        await self._finish_before_cancellation(persist())

    @staticmethod
    def _error_text(exc: Exception) -> str:
        del exc
        return "Replay could not ingest the next fleet tick. Retry or reset."

    def _events_processed(self) -> int:
        return sum(len(tick) for tick in self.ticks[: self.current])

    def _status_locked(self) -> dict[str, Any]:
        total = len(self.ticks)
        progress = round((self.current / total) * 100, 1) if total else 100.0
        events_processed = self._events_processed()
        return {
            "state": self.state,
            "status": self.state,
            "current": self.current,
            "total": total,
            "progress": progress,
            # Keep the v1 spelling as an additive compatibility alias. Both
            # values now intentionally mean fleet ticks per second.
            "events_per_second": self.ticks_per_second,
            "ticks_per_second": self.ticks_per_second,
            "current_tick": self.current,
            "total_ticks": total,
            "events_processed": events_processed,
            "total_events": len(self.events),
            "last_error": self.last_error,
            "revision": self.revision,
        }

    def status(self) -> dict[str, Any]:
        """Best-effort synchronous compatibility view for startup integrations."""

        return self._status_locked()

    async def dashboard_revisions(self) -> tuple[int, int]:
        async with self._lock:
            store_revision = await run_in_threadpool(self.store.current_revision)
            return store_revision, self.revision

    async def dashboard_snapshot(self) -> tuple[dict[str, Any], dict[str, Any]]:
        async with self._lock:
            snapshot = await run_in_threadpool(self.store.dashboard_snapshot)
            return snapshot, self._status_locked()

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self.revision:
            raise ReplayRevisionConflict(self.revision)

    def _detach_task_locked(self) -> asyncio.Task[None] | None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        return task

    async def start(
        self,
        ticks_per_second: float | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self._finish_before_cancellation(
            self._start(
                ticks_per_second,
                expected_revision=expected_revision,
            )
        )

    async def _start(
        self,
        ticks_per_second: float | None = None,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        detached: asyncio.Task[None] | None = None
        async with self._lock:
            self._check_revision(expected_revision)
            rate = self.ticks_per_second if ticks_per_second is None else ticks_per_second
            changed = self.ticks_per_second != rate
            if self.current >= len(self.ticks):
                changed = changed or self.state != "complete" or self.last_error is not None
                if changed:
                    await self._touch()
                self.ticks_per_second = rate
                self.state = "complete"
                self.last_error = None
                return self._status_locked()
            changed = changed or self.state != "running" or self.last_error is not None
            if changed:
                await self._touch()
            if self._task is not None:
                detached = self._detach_task_locked()
            self.ticks_per_second = rate
            self.state = "running"
            self.last_error = None
            self._task = asyncio.create_task(self._run(), name="fleetwatch-replay")
            result = self._status_locked()
        await self._consume_cancelled(detached)
        return result

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while True:
                async with self._lock:
                    if self.state != "running":
                        return
                    if self.current >= len(self.ticks):
                        if self.loop:

                            async def rewind() -> None:
                                await run_in_threadpool(self.store.clear)
                                self.current = 0

                            try:
                                await self._finish_before_cancellation(rewind())
                            except Exception as exc:
                                LOGGER.exception("Replay loop reset failed")
                                try:
                                    await self._touch()
                                except ReplayOperationError:
                                    pass
                                self.state = "error"
                                self.last_error = self._error_text(exc)
                                return
                        else:
                            self.state = "complete"
                            return
                    tick = self.ticks[self.current]

                    async def ingest_tick(
                        tick_to_ingest: list[dict[str, Any]] = tick,
                    ) -> None:
                        await run_in_threadpool(
                            self.store.ingest_events,
                            tick_to_ingest,
                            trusted=True,
                        )
                        self.current += 1
                        self.last_error = None
                        if self.current >= len(self.ticks) and not self.loop:
                            self.state = "complete"

                    try:
                        await self._finish_before_cancellation(ingest_tick())
                    except Exception as exc:
                        LOGGER.exception("Replay failed at fleet tick %s", self.current)
                        try:
                            await self._touch()
                        except ReplayOperationError:
                            pass
                        self.state = "error"
                        self.last_error = self._error_text(exc)
                        return
                    if self.state == "complete":
                        return
                    deadline += 1.0 / self.ticks_per_second
                    delay = max(0.0, deadline - loop.time())
                await asyncio.sleep(delay)
        finally:
            current_task = asyncio.current_task()
            if self._task is current_task:
                self._task = None
                if self.state == "running":
                    self.state = "paused"

    async def pause(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        return await self._finish_before_cancellation(
            self._pause(expected_revision=expected_revision)
        )

    async def _pause(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        async with self._lock:
            self._check_revision(expected_revision)
            if self.state == "running":
                await self._touch()
            task = self._detach_task_locked()
            if self.state == "running":
                self.state = "paused"
            result = self._status_locked()
        await self._consume_cancelled(task)
        return result

    async def step(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        return await self._finish_before_cancellation(
            self._step(expected_revision=expected_revision)
        )

    async def _step(self, *, expected_revision: int | None = None) -> dict[str, Any]:
        async with self._lock:
            self._check_revision(expected_revision)
            if self.current >= len(self.ticks):
                if self.state != "complete" or self.last_error is not None:
                    await self._touch()
                    self.state = "complete"
                    self.last_error = None
                result = self._status_locked()
                task = self._detach_task_locked()
            else:
                await self._touch()
                task = self._detach_task_locked()
                self.state = "paused"
                self.last_error = None
                tick = self.ticks[self.current]

                async def ingest_tick() -> dict[str, Any]:
                    await run_in_threadpool(self.store.ingest_events, tick, trusted=True)
                    self.current += 1
                    self.state = "complete" if self.current >= len(self.ticks) else "paused"
                    return self._status_locked()

                try:
                    result = await self._finish_before_cancellation(ingest_tick())
                except Exception as exc:
                    LOGGER.exception("Replay step failed at fleet tick %s", self.current)
                    self.state = "error"
                    self.last_error = self._error_text(exc)
                    raise ReplayOperationError(self.last_error) from exc
        await self._consume_cancelled(task)
        return result

    async def restart(
        self,
        ticks_per_second: float | None = None,
        *,
        expected_revision: int | None = None,
        prime_first_tick: bool = False,
    ) -> dict[str, Any]:
        return await self._finish_before_cancellation(
            self._restart(
                ticks_per_second,
                expected_revision=expected_revision,
                prime_first_tick=prime_first_tick,
            )
        )

    async def _restart(
        self,
        ticks_per_second: float | None = None,
        *,
        expected_revision: int | None = None,
        prime_first_tick: bool = False,
    ) -> dict[str, Any]:
        failure: ReplayOperationError | None = None
        async with self._lock:
            self._check_revision(expected_revision)
            await self._touch()
            task = self._detach_task_locked()

            async def clear_and_restart() -> None:
                await run_in_threadpool(self.store.clear)
                self.current = 0
                if prime_first_tick:
                    await run_in_threadpool(
                        self.store.ingest_events,
                        self.ticks[0],
                        trusted=True,
                    )
                    self.current = 1
                if ticks_per_second is not None:
                    self.ticks_per_second = ticks_per_second
                self.last_error = None
                self.state = "running"
                self._task = asyncio.create_task(self._run(), name="fleetwatch-replay")

            try:
                await self._finish_before_cancellation(clear_and_restart())
            except Exception as exc:
                LOGGER.exception("Replay restart failed")
                self.state = "error"
                self.last_error = self._error_text(exc)
                failure = ReplayOperationError(self.last_error)
                failure.__cause__ = exc
            result = self._status_locked()
        await self._consume_cancelled(task)
        if failure is not None:
            raise failure
        return result

    @staticmethod
    async def _consume_cancelled(task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        caller = asyncio.current_task()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if caller is None or caller.cancelling() == 0:
                return
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            raise cancellation

    async def shutdown(self) -> None:
        await self._finish_before_cancellation(self._shutdown())

    async def _shutdown(self) -> None:
        async with self._lock:
            if self.state == "running":
                try:
                    await self._touch()
                except ReplayOperationError:
                    pass
                self.state = "paused"
            task = self._detach_task_locked()
        await self._consume_cancelled(task)
