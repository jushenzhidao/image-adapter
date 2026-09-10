"""Unit coverage for an exit that cannot be reached from the HTTP surface.

Today no route declares a required parameter, so FastAPI's validation handler
is never triggered: a malformed channel configuration is reported by
channel.py as ``channel_config_error`` instead. Testing the handler directly
keeps its branch covered for the day an endpoint adds a required parameter.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from adapter.error_handlers import on_http_exception, on_validation_error


def _body(resp) -> dict:
    """The error handlers return a bare Response, which has no .json() helper
    (that lives on the test client's response, not Starlette's)."""
    return json.loads(resp.body)


def _request(request_id: str | None = "rid") -> Request:
    scope: dict = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [],
        "query_string": b"",
    }
    if request_id:
        scope["state"] = {"request_id": request_id}
    return Request(scope)


def test_validation_error_becomes_a_400_envelope():
    exc = RequestValidationError(
        [
            {
                "loc": ("body", "n"),
                "msg": "Input should be a valid integer",
                "type": "int_parsing",
            }
        ]
    )
    resp = asyncio.run(on_validation_error(_request(), exc))

    assert resp.status_code == 400
    body = _body(resp)["error"]
    assert body["code"] == "invalid_request"
    assert body["type"] == "invalid_request_error"
    # The transport marker is dropped: OpenAI's `param` names the field.
    assert body["param"] == "n"


def test_http_exception_maps_status_to_a_stable_code():
    resp = asyncio.run(on_http_exception(_request(), HTTPException(404, "Not Found")))

    assert resp.status_code == 404
    body = _body(resp)["error"]
    assert body["code"] == "not_found"
    assert body["type"] == "invalid_request_error"


def test_unmapped_status_still_gets_a_stable_code():
    resp = asyncio.run(on_http_exception(_request(), HTTPException(418, "teapot")))
    assert _body(resp)["error"]["code"] == "http_418"


def test_server_status_is_classified_as_a_server_error():
    resp = asyncio.run(on_http_exception(_request(), HTTPException(503, "down")))
    assert _body(resp)["error"]["type"] == "server_error"


def test_request_id_is_echoed_into_the_response_headers():
    resp = asyncio.run(on_http_exception(_request("abc"), HTTPException(404, "x")))
    assert resp.headers["X-Request-Id"] == "abc"
