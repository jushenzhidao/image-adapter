"""Concurrent materialisation: apply one awaitable-producing call to N items.

Three facts decide this module's shape, and all three are load-bearing.

**It has to live behind ctx, not in the scripts.** ``sandbox.ALLOWED_STDLIB`` is
a stdlib-only whitelist that deliberately excludes both ``asyncio`` and adapter
internals, so a script cannot import this module or ``gather`` for itself. The
fan-out primitive is therefore engine-side and a script reaches it through a
ctx helper.

**``asyncio.TaskGroup`` is not usable.** A child that raises wraps the exception
in an ``ExceptionGroup``, and ``executor._call_phase`` re-raises ``AdapterError``
untouched while collapsing everything else into ``ScriptRuntimeError`` (500). A
script's ``ctx.fail()`` -- a 400 the caller is meant to read -- would come back
as a 500. ``gather(return_exceptions=True)`` returns the exception *objects*
instead of raising, which leaves the engine's handling untouched.

**Fail-fast order has to be preserved.** A serial loop reports the error of the
earliest item it reached. Re-raising the first *exception in input order* -- not
the first that happens to finish, and not a preferred exception type -- is the
faithful concurrent analogue, so a request that failed for one reason serially
does not start failing for another reason once it is fanned out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


async def fanout(
    work: Callable[[Any], Awaitable[Any]],
    items: Iterable[Any],
    *,
    limit: int,
) -> list[Any]:
    """Applies ``work`` to every item, at most ``limit`` at a time, in order.

    Returns one result per item, ``result[i]`` belonging to ``items[i]``
    whatever order they finished in. Raises the failure of the earliest failing
    item, with its own type and therefore its own status: an ``AdapterError``
    from ``ctx.fail()`` stays a 400, anything else is wrapped by the caller
    exactly as it would be on the serial path.

    ``limit`` is clamped to at least 1 rather than rejected. A configured 0 is a
    deployment mistake, but deadlocking every request that fetches two images is
    a worse way to report it than running them one at a time.
    """
    pending = list(items)
    if not pending:
        return []

    width = max(1, limit)
    if width == 1 or len(pending) == 1:
        # Still one at a time, but through the same shape so the caller never
        # has to care which arm ran.
        outcomes: list[Any] = [await work(item) for item in pending]
    else:
        gate = asyncio.Semaphore(width)

        async def bounded(item: Any) -> Any:
            async with gate:
                return await work(item)

        outcomes = await asyncio.gather(
            *(bounded(item) for item in pending), return_exceptions=True
        )

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # `raise outcome` rather than `raise ... from outcome`: the traceback
            # the caller sees should start here, the same way a serial loop's
            # would, and not claim a frame that never existed.
            raise outcome
    return outcomes
