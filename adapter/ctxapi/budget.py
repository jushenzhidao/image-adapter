"""Read-only projection of the cascade budget onto ctx.

Both properties return ``None`` on the single-stage path, where no budget
exists. The stage runner attaches one via ``attach_budget``.
"""

from __future__ import annotations

from typing import Any

from adapter.ctxapi.base import CtxMixin


class BudgetMixin(CtxMixin):
    """``ctx.remaining`` / ``ctx.deadline`` for scripts that pace themselves."""

    # Annotation only: ContextCore.__init__ owns the value.
    _budget: Any

    def attach_budget(self, budget: Any) -> None:
        """Engine-side setter, so the stage runner touches no private name."""
        self._budget = budget

    @property
    def remaining(self) -> float | None:
        """Seconds left in the cascade budget; None on the single-stage path."""
        if self._budget is None:
            return None
        remaining: float = self._budget.remaining
        return remaining

    @property
    def deadline(self) -> float | None:
        """Monotonic instant the cascade budget expires, or None."""
        if self._budget is None:
            return None
        started: float = self._budget.started
        total: float = self._budget.total
        return started + total
