"""SSE streaming bridge: split a complete non-streaming chat completion into
OpenAI-compatible SSE chunks (AC-07)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

CHUNK_SIZE = 8  # characters per content delta chunk


def _chunk_envelope(chunk_id: str, created: int, model: str, delta: dict, finish: str | None) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_chat_as_sse(completion: dict) -> AsyncIterator[str]:
    """Yields SSE lines: role chunk, content slices, finish chunk, [DONE]."""
    chunk_id = completion.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = completion.get("created") or int(time.time())
    model = completion.get("model", "")
    content = ""
    choices = completion.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""

    yield _chunk_envelope(chunk_id, created, model, {"role": "assistant"}, None)

    for i in range(0, len(content), CHUNK_SIZE):
        piece = content[i : i + CHUNK_SIZE]
        yield _chunk_envelope(chunk_id, created, model, {"content": piece}, None)

    yield _chunk_envelope(chunk_id, created, model, {}, "stop")
    yield "data: [DONE]\n\n"
