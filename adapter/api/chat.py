"""POST /v1/chat/completions

Non-streaming requests return a ChatCompletion object. With stream=true the
adapter bridges: the script yields one complete response and the gateway slices
it into SSE chunks, so a non-streaming upstream still looks streaming.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from adapter.api.common import json_ok
from adapter.api.pipeline import adapt
from adapter.errors import InvalidRequestError
from adapter.utils.sse import stream_chat_as_sse


def _validate(body: dict) -> None:
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        raise InvalidRequestError(
            "'messages' must be a non-empty list", param="messages"
        )


async def chat_handler(request: Request) -> Response:
    outcome = await adapt(request, "chat", _validate)

    if not outcome.stream:
        data = outcome.payload if isinstance(outcome.payload, dict) else {}
        data.setdefault("created", int(time.time()))
        data.setdefault("object", "chat.completion")
        return json_ok(data, outcome.request_id, outcome.headers)

    return StreamingResponse(
        stream_chat_as_sse(outcome.payload),
        media_type="text/event-stream",
        headers=outcome.headers,
    )
