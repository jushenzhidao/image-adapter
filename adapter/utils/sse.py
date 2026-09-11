"""SSE streaming bridge: split a complete non-streaming chat completion into
OpenAI-compatible SSE chunks (AC-07)."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator

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


def _deltas(content: object) -> Iterator[dict]:
    """One content value -> the deltas that carry it.

    A string -- what a chat-speaking script returns -- slices as it always has.

    A list of parts (an image front door's reply) is a different problem: text
    can still be sliced, but an image cannot be cut into eight-character pieces,
    so each image part goes out whole as its own delta. A client therefore has
    to treat a non-string `delta.content` as "append this part" -- which it
    already has to do for the non-streaming reply, where the same content is an
    array.
    """
    if isinstance(content, str):
        for start in range(0, len(content), CHUNK_SIZE):
            yield {"content": content[start : start + CHUNK_SIZE]}
        return

    if not isinstance(content, list):
        return

    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = str(part.get("text") or "")
            for start in range(0, len(text), CHUNK_SIZE):
                yield {"content": text[start : start + CHUNK_SIZE]}
        else:
            yield {"content": [part]}


async def stream_chat_as_sse(completion: dict) -> AsyncIterator[str]:
    """Yields SSE lines: role chunk, content deltas, finish chunk, [DONE]."""
    chunk_id = completion.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = completion.get("created") or int(time.time())
    model = completion.get("model", "")
    content: object = ""
    choices = completion.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""

    yield _chunk_envelope(chunk_id, created, model, {"role": "assistant"}, None)

    for delta in _deltas(content):
        yield _chunk_envelope(chunk_id, created, model, delta, None)

    yield _chunk_envelope(chunk_id, created, model, {}, "stop")
    yield "data: [DONE]\n\n"
