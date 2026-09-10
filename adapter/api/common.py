"""Shared helpers for API handlers: body parsing, response shaping.

Error handling deliberately does *not* live here. It is registered globally in
``adapter/error_handlers.py``, which also covers the exits a per-handler
decorator cannot reach: router-level 404/405 and request validation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from starlette.requests import Request

from adapter.errors import InvalidRequestError
from adapter.jsoncodec import JSON_LIBRARY, JSONResponse, loads

logger = logging.getLogger(__name__)

#: Which encoder actually parses and renders JSON. Reported once at startup so
#: a deployment can tell at a glance whether the fast path is live.
JSON_PARSER = JSON_LIBRARY

# Below this size parsing costs less than the thread hand-off, so small
# requests stay inline. Above it the parse is pure CPU on the event loop: a
# 5 MB body (a base64 image) measures ~5 ms, and every other coroutine in the
# worker waits behind it.
_INLINE_PARSE_LIMIT = 64 * 1024


async def parse_json_body(request: Request) -> dict:
    raw = await request.body()
    try:
        if len(raw) <= _INLINE_PARSE_LIMIT:
            body = loads(raw)
        else:
            body = await asyncio.to_thread(loads, raw)
    except ValueError:
        # json.JSONDecodeError and orjson.JSONDecodeError both subclass this.
        raise InvalidRequestError("Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object")
    return body


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def json_ok(
    data: dict, request_id: str, extra_headers: dict[str, str] | None = None
) -> JSONResponse:
    headers = {"X-Request-Id": request_id}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(data, headers=headers)
