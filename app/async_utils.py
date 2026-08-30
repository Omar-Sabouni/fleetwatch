"""Small cross-event-loop executor used by the SQLite facade."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fleetwatch-store")
ResultT = TypeVar("ResultT")


async def finish_before_cancellation(operation: Awaitable[ResultT]) -> ResultT:
    """Finish one logical operation before caller cancellation can escape."""

    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except asyncio.CancelledError:
            raise cancellation from None
        except Exception:
            pass
        raise cancellation


async def run_sync(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable without AnyIO portal/event-loop affinity.

    The process-wide executor is safe across the separate event loops used by
    lifespan-less ASGI transports. Once a worker begins, cancellation is
    deferred until it has finished so callers cannot release resources while a
    SQLite operation is still using them. Work still queued in the bounded pool
    is cancelled immediately.
    """

    call = partial(function, *args, **kwargs)
    future = _EXECUTOR.submit(call)
    cancellation: asyncio.CancelledError | None = None
    # A tiny timer-based poll is deliberate: it remains reliable in restricted
    # runtimes where cross-thread event-loop wakeups are blocked.
    while not future.done():
        try:
            await asyncio.sleep(0.001)
        except asyncio.CancelledError as exc:
            if future.cancel():
                raise
            cancellation = exc
    if cancellation is not None:
        try:
            future.result()
        except Exception:
            pass
        raise cancellation
    return future.result()
