"""The concurrent-materialisation primitive: its guarantees are its whole value.

Four of them, and each is pinned by a test that a plausible implementation would
fail:

  * input order survives out-of-order completion,
  * at most ``limit`` calls are in flight,
  * the *earliest failing item* decides, with its own exception type -- so a
    request that failed one way serially cannot fail another way fanned out,
  * and no ``ExceptionGroup`` is ever raised.

The last one is the trap that makes ``asyncio.TaskGroup`` unusable here: an
``ExceptionGroup`` does not match ``executor._call_phase``'s ``except
AdapterError``, so ``ctx.fail()``'s 400 would come back as a 500. A test that
only checked "the failure propagates" would pass with TaskGroup and ship that.
"""

from __future__ import annotations

import asyncio

import pytest

from adapter.errors import AdapterError
from adapter.utils.fanout import fanout

LIMIT = 3


async def test_results_keep_input_order_however_they_finish():
    """The caller zips results against its own input; completion order is an
    implementation detail it must never have to know about."""

    async def work(item: int) -> int:
        # Descending delay, so the last item finishes first and an
        # "append as completed" implementation would come back reversed.
        await asyncio.sleep(0.01 * (5 - item))
        return item * 10

    assert await fanout(work, [0, 1, 2, 3, 4], limit=LIMIT) == [0, 10, 20, 30, 40]


async def test_no_more_than_the_limit_are_in_flight():
    """The cap is the reason the helper exists rather than a bare gather: the
    connection pool is a resource ceiling, not a throttle, so an uncapped fan-out
    queues inside aiohttp and pays that queueing inside each action's timeout."""
    in_flight = 0
    peak = 0

    async def work(item: int) -> int:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return item

    await fanout(work, list(range(9)), limit=LIMIT)

    assert peak == LIMIT, f"peak concurrency was {peak}, expected exactly {LIMIT}"


async def test_the_earliest_failure_in_input_order_decides():
    """A serial loop fails at the first item it reaches, whatever that item
    raised. Fanned out, the answer must be the same -- not the first to finish,
    and not whichever failure reads better."""

    async def work(item: str) -> str:
        if item == "first":
            await asyncio.sleep(0.05)
            raise ValueError("the earliest item is broken")
        await asyncio.sleep(0.01)
        raise AdapterError(400, "a later item was refused", "invalid_request_error")

    with pytest.raises(ValueError):
        await fanout(work, ["first", "second"], limit=LIMIT)


async def test_an_adapter_error_keeps_its_status():
    """Preferring an AdapterError over an earlier ordinary failure would hide a
    real script bug behind a client-facing 400, which is the opposite of what the
    engine's error handling is for."""

    async def work(item: int) -> int:
        raise AdapterError(
            400, "the image is unusable", "invalid_request_error", "image", "image_invalid"
        )

    with pytest.raises(AdapterError) as excinfo:
        await fanout(work, [0], limit=LIMIT)

    assert (excinfo.value.status, excinfo.value.code) == (400, "image_invalid")


async def test_a_failure_never_arrives_wrapped_in_an_exception_group():
    """The TaskGroup trap, stated as an assertion.

    ``_call_phase`` re-raises ``AdapterError`` untouched and collapses everything
    else into ``ScriptRuntimeError`` (500). An ``ExceptionGroup`` is not an
    ``AdapterError``, so a wrapped 400 reaches the client as a 500.
    """

    async def work(item: int) -> int:
        if item == 1:
            raise AdapterError(400, "refused", "invalid_request_error", None, "nope")
        return item

    with pytest.raises(AdapterError) as excinfo:
        await fanout(work, [0, 1, 2], limit=LIMIT)

    assert not isinstance(excinfo.value, BaseExceptionGroup)


async def test_a_limit_below_one_runs_serially_rather_than_deadlocking():
    """A configured 0 is a deployment mistake; hanging every request that fetches
    two images would be a worse way to report it than running them one by one."""
    calls = 0

    async def work(item: int) -> int:
        nonlocal calls
        calls += 1
        return item

    assert await fanout(work, [1, 2, 3], limit=0) == [1, 2, 3]
    assert calls == 3


async def test_no_items_makes_no_calls():
    async def work(item: int) -> int:
        raise AssertionError("work must not be called for an empty input")

    assert await fanout(work, [], limit=LIMIT) == []


async def test_a_single_item_takes_the_serial_path_unchanged():
    """One item is the N=1 case of every fan-out site, and it must behave
    exactly as the loop it replaced -- same return, same exception type."""

    async def ok(item: int) -> int:
        return item * 2

    assert await fanout(ok, [21], limit=LIMIT) == [42]

    async def boom(item: int) -> int:
        raise AdapterError(413, "too large", "invalid_request_error", "image")

    with pytest.raises(AdapterError) as excinfo:
        await fanout(boom, [0], limit=LIMIT)

    assert excinfo.value.status == 413
