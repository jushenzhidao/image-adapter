"""Request logging middleware: assigns request_id (BR-010) and logs
method/path/status/duration for every request.

Raw ASGI rather than Starlette's ``BaseHTTPMiddleware``. The latter runs the
rest of the stack inside an anyio task group with an in-memory object stream
between itself and the app, which measures roughly +0.4 ms per request -- about
twenty times the cost of the framework layer it wraps, paid on every request.
A raw middleware only rewrites the send channel.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("adapter.access")

_REQUEST_ID = b"x-request-id"
# Used when the app raised before producing a response: the access log must
# still show something, and a 5xx is the honest guess.
_FALLBACK_STATUS = 500


class LoggingMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Honour an inbound id so the control plane can correlate a request
        # with its own logs, otherwise mint one (BR-010).
        inbound = dict(scope["headers"]).get(_REQUEST_ID)
        request_id = inbound.decode("latin-1") if inbound else str(uuid.uuid4())
        # Starlette's Request.state reads scope["state"], so writing here is
        # what makes request.state.request_id visible to handlers and to the
        # error handlers.
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.monotonic()
        status = _FALLBACK_STATUS

        async def send_with_request_id(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = message.setdefault("headers", [])
                # Never clobber an id a handler set deliberately.
                if not any(key.lower() == _REQUEST_ID for key, _ in headers):
                    headers.append((_REQUEST_ID, request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Logged in `finally` so a failure that never produced a response
            # still appears in the access log.
            logger.info(
                "%s %s -> %d (%.1fms) request_id=%s",
                scope["method"],
                scope["path"],
                status,
                (time.monotonic() - started) * 1000,
                request_id,
            )
