"""Shared helpers for API handlers: body parsing, error wrapping."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from adapter.errors import (
    AdapterError,
    InvalidRequestError,
    error_response,
    internal_error_response,
)

logger = logging.getLogger(__name__)


async def parse_json_body(request: Request) -> dict:
    try:
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        raise InvalidRequestError("Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise InvalidRequestError("Request body must be a JSON object")
    return body


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or str(uuid.uuid4())


def handle_errors(
    func: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Wraps a handler with the unified OpenAI error format (BR-009)."""

    async def wrapper(request: Request) -> Response:
        request_id = get_request_id(request)
        try:
            return await func(request)
        except AdapterError as exc:
            logger.warning(
                "request failed: %s (code=%s, request_id=%s)",
                exc.message,
                exc.code,
                request_id,
            )
            return error_response(exc, request_id)
        except Exception:
            logger.exception("unhandled error (request_id=%s)", request_id)
            return internal_error_response(request_id)

    return wrapper


def json_ok(
    data: dict, request_id: str, extra_headers: dict[str, str] | None = None
) -> JSONResponse:
    headers = {"X-Request-Id": request_id}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(data, headers=headers)
