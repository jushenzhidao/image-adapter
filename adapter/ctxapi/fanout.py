"""Concurrent materialisation, handed to scripts as one ctx call.

**Why a script cannot do this itself.** ``sandbox.ALLOWED_STDLIB`` is a
stdlib-only whitelist that deliberately excludes ``asyncio``, and adapter
internals are never importable from a script either. Measured: ``import
asyncio``, ``concurrent.futures``, ``anyio`` and ``httpx`` are all rejected with
``Forbidden import``. A script therefore has no way to fan out at all, so the
engine has to lend it the one primitive it cannot build.

**What it is for.** Reading the client's own image references, and moving images
in and out of object storage -- "materialisation". ``docs/07`` §14.2.1 draws the
line between that and *orchestration*, which stays excluded, and the three
criteria it states hold here by construction rather than by convention:

  * *no extra upstream generation calls* -- generation calls are engine-owned
    (``executor._do_upstream``) and unreachable from a script: ``ctx.emit()``
    only sets the plan for the single call the engine then makes. Nothing
    reachable through this helper can add one, so a request's cost is unchanged.
  * *failure semantics unchanged* -- ``adapter.utils.fanout`` re-raises the
    earliest failing item's own exception, so an ``ctx.fail()`` inside ``work``
    still reaches the client as its own status.
  * *a bounded degree* -- ``fanout_concurrency``, enforced by that same helper.

**What it is not.** Not a general concurrency tool for the request pipeline, and
not a replacement for ``STAGES``, which stays strictly serial because its
degradation and budget semantics depend on order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from adapter.ctxapi.base import CtxMixin
from adapter.utils.fanout import fanout


class FanoutMixin(CtxMixin):
    """Runs one per-item operation on several items at once."""

    async def fanout(
        self,
        items: Iterable[Any],
        work: Callable[[Any], Awaitable[Any]],
    ) -> list[Any]:
        """Applies ``work`` to every item concurrently, preserving input order.

        ``work`` is an ordinary async callable, and it is deliberately allowed
        to close over ``ctx``: that is what makes a two-step conversion
        expressible -- fetch a reference, then re-host the bytes -- without this
        helper having to know which of the two steps a channel needs.

        Three things a caller may rely on, and should: ``result[i]`` belongs to
        ``items[i]`` whatever order they finished in; at most
        ``fanout_concurrency`` items are in flight; and the earliest failure in
        input order decides, keeping its own exception type, so a request that
        would fail a given way in a loop still fails that way here.

        An empty ``items`` makes no calls and returns an empty list. One item is
        the degenerate case of every fan-out site and takes the serial path, so
        N=1 behaves exactly as the loop it replaced.
        """
        return await fanout(work, items, limit=self.settings.fanout_concurrency)
