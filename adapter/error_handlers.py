"""Global exception handlers: one OpenAI error envelope for every exit.

BR-009 requires the OpenAI error shape on every response, and the exits are
not all in one place:

    - ``AdapterError`` raised inside a handler is the obvious one.
    - 404 and 405 come from the router *before* any handler runs. Starlette's
      defaults are ``PlainTextResponse``, so a client calling
      ``response.json()`` gets a parse error rather than an error message.
    - 422 comes from FastAPI's own request validation.
    - Anything unhandled reaches the ASGI server's own 500.

Registering handlers here makes the envelope a property of the application
rather than of five decorated functions. That distinction is the whole point:
a decorator only wraps the callable it decorates, which is why the previous
``@handle_errors`` approach could never cover the router-level exits.

Handler placement follows Starlette's split: ``Exception`` is collected by
``ServerErrorMiddleware`` (outermost, so it also catches middleware faults),
everything else by ``ExceptionMiddleware`` (just inside the router).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from adapter.api.common import get_request_id
from adapter.errors import (
    AdapterError,
    InvalidRequestError,
    error_response,
    internal_error_response,
)

logger = logging.getLogger(__name__)

# Starlette's 404/405 carry no machine-readable code, so map the status onto
# the closest OpenAI-style one. Anything unmapped still gets a stable
# "http_<status>" so clients can switch on it.
_STATUS_CODE_MAP = {
    400: "invalid_request",
    401: "invalid_api_key",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limit_exceeded",
}


def _classify(status: int) -> str:
    if status < 500:
        return "invalid_request_error"
    if status == 529:
        return "overloaded_error"
    return "server_error"


async def on_adapter_error(request: Request, exc: AdapterError) -> Response:
    """Domain errors: already carry status, message, type, param and code."""
    request_id = get_request_id(request)
    logger.warning(
        "request failed: %s (code=%s, request_id=%s)",
        exc.message,
        exc.code,
        request_id,
    )
    return error_response(exc, request_id)


async def on_http_exception(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Router-level exits (404/405/...) and any manually raised HTTPException."""
    status = exc.status_code
    detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    error = AdapterError(
        status,
        detail,
        _classify(status),
        None,
        _STATUS_CODE_MAP.get(status, f"http_{status}"),
    )
    return error_response(error, get_request_id(request))


async def on_validation_error(
    request: Request, exc: RequestValidationError
) -> Response:
    """FastAPI request validation.

    Only reachable if a route declares a *required* validated parameter. Every
    channel header is declared optional by design, so a malformed channel
    configuration is normally reported by ``channel.py`` as
    ``channel_config_error`` (400) rather than here. The handler exists so the
    envelope holds even if a future endpoint adds a required parameter.
    """
    errors = exc.errors() or [{}]
    first = errors[0]
    # Drop the leading "body"/"query"/"header" marker: the OpenAI `param`
    # field names the offending field, not its transport.
    parts = [str(p) for p in first.get("loc", ()) if p not in ("body", "query")]
    param = ".".join(parts) or None
    error = InvalidRequestError(first.get("msg", "Invalid request"), param=param)
    return error_response(error, get_request_id(request))


async def on_unhandled(request: Request, exc: Exception) -> Response:
    """Last resort. Never leaks the traceback to the client (BR-009)."""
    request_id = get_request_id(request)
    logger.exception("unhandled error (request_id=%s)", request_id)
    return internal_error_response(request_id)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AdapterError, on_adapter_error)
    app.add_exception_handler(StarletteHTTPException, on_http_exception)
    app.add_exception_handler(RequestValidationError, on_validation_error)
    app.add_exception_handler(Exception, on_unhandled)
