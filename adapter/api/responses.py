"""POST /v1/responses

Agent-style orchestration: the script decides how to satisfy the request (a
common shape is prompt rewrite -> image generation -> output array).

State chain: previous_response_id loads the prior turn from the state store
before the script runs, and the new turn is persisted with a TTL afterwards.
"""

from __future__ import annotations

import time
import uuid

from starlette.requests import Request
from starlette.responses import Response

from adapter.api.common import json_ok
from adapter.api.pipeline import adapt
from adapter.errors import InvalidRequestError
from adapter.state_store import StateStore

CTX_PREFIX = "resp_ctx:"


async def responses_handler(request: Request) -> Response:
    store: StateStore = request.app.state.state_store

    async def prepare(body: dict) -> None:
        user_input = body.get("input")
        if not user_input or not isinstance(user_input, str):
            raise InvalidRequestError(
                "'input' must be a non-empty string", param="input"
            )

        previous_id = body.get("previous_response_id")
        if previous_id:
            prior = await store.get(f"{CTX_PREFIX}{previous_id}")
            if prior:
                body["_previous_ctx"] = prior

    outcome = await adapt(request, "response", prepare)

    data = outcome.payload if isinstance(outcome.payload, dict) else {}
    response_id = data.get("id") or f"resp-{uuid.uuid4().hex[:24]}"
    data.setdefault("id", response_id)
    data.setdefault("created", int(time.time()))
    data.setdefault("object", "response")

    ttl = request.app.state.settings.resp_ctx_ttl
    await store.set(
        f"{CTX_PREFIX}{response_id}",
        {"response_id": response_id, "output": data.get("output", [])},
        ttl=ttl,
    )

    return json_ok(data, outcome.request_id, outcome.headers)
