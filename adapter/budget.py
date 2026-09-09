"""Shared time budget for multi-stage pipelines (BR-011, AC-22 / AC-27).

A single wall-clock cap per phase is enough for one upstream call, but a
cascade makes N of them: independent per-stage timeouts add up and can run far
past what the client is willing to wait. So the stages share one budget, and
every outbound call takes the smaller of its own timeout and what is left.

The three layers are deliberately separate:

  script_timeout   each transform() call            30s
  stage_timeout    one stage's upstream call       120s
  budget.total     the whole cascade               300s (ceiling 600s)
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
