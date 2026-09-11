"""Shared time budget for multi-stage pipelines (BR-011, AC-22 / AC-27).

A single wall-clock cap per phase is enough for one upstream call, but a
cascade makes N of them: independent per-stage timeouts add up and can run far
past what the client is willing to wait. So the stages share one budget, and
every outbound call takes the smaller of its own timeout and what is left.

Four bounds apply to one request and they are deliberately separate, because
which one fired is what the caller needs to tell apart (AC-27). Code defaults
are listed; the shipped .env raises `upstream_timeout` to 180s and lowers
`poll_timeout_default` to 120s.

  script_timeout        each transform() call        30s   script_timeout
  upstream_timeout      one upstream HTTP call       60s   upstream_timeout
  poll_timeout_default  one async job, all polls     300s  poll_timeout
  budget.total          the whole cascade            300s  pipeline_timeout
                                                          (ceiling 600s)

`upstream_timeout` is the bound that decides how long a generation may take,
and the only one worth raising for a slow model. A script may override it per
call through ctx.emit(timeout=...); this module is what keeps that override
honest, because N stages each spending a full timeout would sum far past what
the client is willing to wait for.

Note there is no per-stage timeout constant: a stage's upstream call is
bounded by `upstream_timeout`, or by the script's override. The
`stage_timeout` error code therefore maps to no setting -- it only
distinguishes a *named* stage's timeout from an unnamed single-call one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Budget:
    """Countdown shared by every stage of one request."""

    total: float
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        """Seconds left, floored at zero so callers never see a negative."""
        return max(0.0, self.total - self.elapsed)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def cap(self, timeout: float) -> float:
        """Clamps one call's timeout to what the cascade can still afford.

        This is what stops per-stage timeouts from summing past `total`.
        """
        return min(timeout, self.remaining)


def resolve_total(
    requested: float | None, default: float, ceiling: float
) -> float:
    """Picks the cascade budget from a caller-supplied value.

    The control plane may ask for more time via X-Stage-Timeout, but not more
    than the deployment's ceiling: the header is attacker-reachable and an
    unbounded budget would pin a worker indefinitely.
    """
    total = default if requested is None else requested
    if total <= 0:
        total = default
    return min(total, ceiling)
