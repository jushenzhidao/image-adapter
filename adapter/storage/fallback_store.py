"""The primary, behind a negative cache.

Two measurements decide this shape (2026-09-11, against this codebase):

  * a bucket that rejects the upload fails the put in **~60 ms**;
  * a minio that is not listening fails it in **~6 s**, because the pool the
    minio builder installs retries five times with backoff
    (``Retry(total=5, backoff_factor=0.2)``).

So a fallback that tried the primary first on every request would add ~6 s to
every upload for as long as the primary was down -- comparable to the upload
itself, which measures 1.3~6 s on the fal side. The negative cache is what
turns that from a per-request tax into a per-cooldown one.

In-process and per-worker, mirroring ``StateStore``'s Redis backoff: there is
nothing to synchronise, and being wrong is bounded. After the primary recovers,
each worker pays at most one slow request -- the one that discovers the
recovery, since the marker expires rather than being cleared by a peer.

What is deliberately *not* hidden: parking the primary logs a warning naming
both backends and the cooldown, **reports a ``storage_fallback`` event to
Logfire** (a failover is a fact worth querying, not only one container's log
line), recovering logs an info, and ``ping`` probes the primary for real (so
``/health`` tracks the primary, not just the pair).

Note the shape this produces: while the primary is parked, every upload is
served by the fallback and therefore carries **that** backend's semantics --
``StoredObject.visibility`` says which. A presigned URL and a public one are
not interchangeable, so a caller that cares must read the field rather than
assume the configured primary produced it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from adapter.storage.base import ObjectStore, StoredObject
from adapter.trace_attrs import record_storage_fallback

logger = logging.getLogger(__name__)

#: Default park duration. Long enough to stop paying the 6 s on every request,
#: short enough that a recovered primary is picked up without an operator. The
#: health probe also clears the park, so a monitored deployment recovers on the
#: probe's cadence rather than this one when the probe runs more often.
DEFAULT_COOLDOWN = 30.0


class FallbackStore:
    """Two stores, one port: try the primary, park it when it fails."""

    def __init__(
        self,
        primary: ObjectStore,
        secondary: ObjectStore,
        *,
        cooldown: float = DEFAULT_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
        name: str | None = None,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._cooldown = cooldown
        #: Injectable so a test can expire the park without sleeping.
        self._clock = clock
        self._parked_until = 0.0
        #: Optional override for the reported name. Needed when the two halves are the
        #: same backend reached two ways (``minio_colocated``), where "a+b" would read
        #: "minio_public+minio_public": true, and useless in /health and log lines.
        self._name = name

    @property
    def name(self) -> str:
        """Both halves, because log lines have to say which one served."""
        return self._name or f"{self._primary.name}+{self._secondary.name}"

    # -- parking -----------------------------------------------------------

    def _parked(self) -> bool:
        return self._clock() < self._parked_until

    def _park(self, detail: str) -> bool:
        """Stop asking the primary until the cooldown expires.

        Returns True when *this* call is the one that parked it, i.e. the transition.
        Callers use that to report the transition rather than every occurrence: the
        store logs a warning on it, and a telemetry event belongs at the same rate
        (see ``_on_primary_failure``) -- otherwise a health probe would emit one event
        per probe for as long as a backend stayed down.

        Logged at warning on the transition only: while parked we never call
        the primary, so the earliest a second warning can appear is one
        cooldown later. That is the intended rate -- frequent enough to be
        visible, too rare to be noise. ``detail`` completes the sentence
        ("... failed (<err>)" / "... reports unusable").
        """
        fresh = not self._parked()
        self._parked_until = self._clock() + self._cooldown
        if fresh:
            # ``extra=`` rather than only prose: Logfire's logging bridge turns these into
            # attributes, so "which store went dark" is a field to filter on rather than a
            # phrase to grep. The message keeps the human-readable version either way.
            logger.warning(
                "object storage primary %s %s; %s serves the next %.0fs",
                self._primary.name,
                detail,
                self._secondary.name,
                self._cooldown,
                extra={
                    "storage_primary": self._primary.name,
                    "storage_secondary": self._secondary.name,
                    "storage_cooldown_s": self._cooldown,
                },
            )
        return fresh

    def _on_primary_failure(self, exc: BaseException | None, *, probe: bool) -> None:
        """Reports the failover to Logfire, naming the two backends.

        The base class reports rather than staying silent, because a failover **changes
        what the caller receives** -- the URL's host, and whether it expires at all, belong
        to whichever backend serves -- so "this pair swapped" is worth a queryable event
        for any pair, not only for the two addresses of one bucket.

        ``address``/``serving`` carry the store names here (``minio_colocated``, ``fal``);
        ``MinioColocatedStore`` overrides this to report the two *addresses* instead, which
        is the more useful pair when both halves are the same backend. ``exc`` is None when
        the primary reported itself unusable instead of raising -- a probe that returns
        False has no exception to quote.
        """
        record_storage_fallback(
            address=self._primary.name,
            serving=self._secondary.name,
            error=exc,
            probe=probe,
        )

    def _unpark(self) -> None:
        if self._parked_until:
            logger.info(
                "object storage primary %s is serving again; %s stands down",
                self._primary.name,
                self._secondary.name,
            )
            self._parked_until = 0.0

    # -- port --------------------------------------------------------------

    async def _attempt(
        self, attempt: Coroutine[Any, Any, StoredObject]
    ) -> StoredObject | None:
        """Run one primary attempt, parking on failure. None means "fell over"."""
        try:
            stored = await attempt
        except Exception as exc:  # noqa: BLE001 - every backend's zoo
            if self._park(f"failed ({exc})"):
                # The transition, not every failed attempt: see _park.
                self._on_primary_failure(exc, probe=False)
            return None
        self._unpark()
        return stored

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        """Primary if it is believed up, otherwise straight to the fallback.

        If both fail the secondary's exception propagates: the caller
        (``StorageMixin``) is the layer that turns a failure into a data URI,
        and swallowing it here would hide that both stores are down.
        """
        if not self._parked():
            stored = await self._attempt(
                self._primary.put(data, key=key, content_type=content_type)
            )
            if stored is not None:
                return stored
        return await self._secondary.put(data, key=key, content_type=content_type)

    async def ping(self) -> bool:
        """True when the pair can serve, probing the primary for real.

        The park is ignored here on purpose: finding out whether the primary is
        back is the whole job of a probe, and it keeps the cache honest -- a
        successful probe clears the park, a failed one sets it. So a monitored
        deployment recovers on the probe's cadence, not this class's.
        """
        healthy = False
        failure: BaseException | None = None
        try:
            healthy = await self._primary.ping()
        except Exception as exc:  # noqa: BLE001 - see put()
            failure = exc

        if healthy:
            self._unpark()
            return True

        # Park it here, quoting the exception when there is one. Doing it in one place
        # keeps the telemetry at the transition -- a primary that stays down is reported
        # once per cooldown, not once per probe, and a k8s probe runs often enough for
        # the difference to matter.
        if failure is not None:
            fresh = self._park(f"failed ({failure})")
        elif not self._parked():
            # ping returned False rather than raising, so nothing has parked it
            # yet. Park it here; there is no exception to quote.
            fresh = self._park("reports unusable")
        else:
            fresh = False
        if fresh:
            self._on_primary_failure(failure, probe=True)

        return await self._secondary.ping()
