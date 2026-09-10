"""``RequestPlan`` and ``ctx.emit()``: how a script steers the outbound call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adapter.ctxapi.base import CtxMixin
from adapter.urlguard import check_url


@dataclass
class RequestPlan:
    """Overrides a script declares for the outbound call via ctx.emit()."""

    url: str | None = None
    method: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_set: bool = False
    form: dict[str, Any] | None = None
    raw: bytes | None = None
    timeout: float | None = None


class PlanMixin(CtxMixin):
    """Declarative outbound-call overrides."""

    plan: RequestPlan

    def reset_plan(self) -> None:
        """Engine-side hook: drops overrides between phases."""
        self.plan = RequestPlan()

    def emit(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        raw: bytes | None = None,
        timeout: float | None = None,
    ) -> None:
        """Declares outbound-call overrides from the request phase.

        Returning a dict from transform() already sets the JSON body, so
        emit() is only needed for anything beyond it: a sub-path on the
        channel URL, a different method, extra headers, multipart, or raw
        bytes.
        """
        plan = self.plan
        if url is not None:
            plan.url = check_url(url, self.settings, header="ctx.emit(url=...)")
        if method is not None:
            plan.method = method.strip().upper()
        if headers:
            plan.headers.update({str(k): str(v) for k, v in headers.items()})
        if query:
            plan.query.update({str(k): str(v) for k, v in query.items()})
        if body is not None:
            plan.body = body
            plan.body_set = True
        if form is not None:
            plan.form = form
        if raw is not None:
            plan.raw = raw
        if timeout is not None:
            plan.timeout = float(timeout)
