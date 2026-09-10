"""Refuses a request body over the configured ceiling, while it is arriving.

A reverse proxy does this earlier and for free (``client_max_body_size``), and
in a deployment that has one it should. This exists because the adapter cannot
assume that: the proxy can be bypassed, and the service is documented to run
standalone as well. Starlette's ``Request.body()`` and ``Request.form()`` both
accumulate with no ceiling of their own -- the multipart route accepts up to 64
files and reads each one into memory -- so without this a caller decides how
much of a worker's memory to consume.

It is one middleware rather than a check inside the handlers because the two
body-reading paths differ (``body()`` for JSON, ``form()`` for multipart), and
a guard on either would miss the other.

Raw ASGI, like the other middlewares; see ``logging.py`` for the measurement
that motivates that choice.
"""

from __future__ import annotations

from typing import Any

from adapter.errors import PayloadTooLargeError, error_response
from adapter.settings import get_settings


class _BodyTooLarge(BaseException):
    """Internal signal. Never escapes this module.

    Inherits ``BaseException`` rather than ``Exception`` on purpose. Handlers
    legitimately wrap body reading in a broad ``except Exception`` -- the
    multipart route turns any parse failure into a 400 -- which would swallow
    this signal and report the wrong error for a body that was simply too big.
    A control-flow signal has to be outside that net to survive code whose only
    sin is being defensive.
    """


class BodyLimitMiddleware:
    """Counts the body as it arrives; the declared length is only a fast path."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._resolve_limit(scope)
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        # An honest oversized upload is refused before a single byte is read,
        # which is the common case and by far the cheapest one.
        if self._declared_over_limit(scope, limit):
            await self._reject(scope, receive, send, limit)
            return

        total = 0
        started = False

        async def capped_receive() -> dict[str, Any]:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, tracked_send)
        except _BodyTooLarge:
            if started:
                # A response is already on the wire; appending a second one
                # would corrupt the stream, so let the error path log it.
                raise
            await self._reject(scope, receive, send, limit)

    @staticmethod
    def _resolve_limit(scope: dict[str, Any]) -> int:
        """Reads the ceiling from the app state, the way the pipeline does.

        Deliberately not from ``get_settings()``: that is a separate,
        environment-derived singleton, so using it would let the middleware and
        the handlers disagree about the limit, and would make the ceiling
        impossible to set from a test that injects settings.
        """
        state = getattr(scope.get("app"), "state", None)
        settings = getattr(state, "settings", None) or get_settings()
        return int(settings.max_request_bytes)

    @staticmethod
    def _declared_over_limit(scope: dict[str, Any], limit: int) -> bool:
        """Reads Content-Length without trusting it.

        Only an honest declaration is acted on. A malformed one is ignored
        rather than rejected here, because the HTTP layer already refuses it
        and the running total covers the body regardless. A chunked upload
        declares nothing at all, which is exactly why that total exists.
        """
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is None:
            return False
        try:
            return int(declared) > limit
        except ValueError:
            return False

    @staticmethod
    async def _reject(
        scope: dict[str, Any], receive: Any, send: Any, limit: int
    ) -> None:
        # The logging middleware runs outside this one and has already put the
        # id on the scope, so the rejection still correlates.
        request_id = (scope.get("state") or {}).get("request_id")
        response = error_response(PayloadTooLargeError(limit), request_id)
        await response(scope, receive, send)
