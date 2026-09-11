"""POST /v1/chat/completions -- a chat-shaped front door onto the image contract.

This route owns no vendor logic. `/v1/images/generations` is the canonical
contract and every script implements only that, so the whole job here is to fold
`messages` into the canonical body and to wrap the canonical reply back into a
ChatCompletion (both live in ``adapter/api/frontdoor.py``).

The folding belongs to the door, not to scripts. An image script reads `prompt`
while a chat body carries `messages`, so leaving it to scripts means every
vendor script reimplementing the same message parsing -- N copies that drift,
and the exact failure this replaced: an empty `parts[0].text`, which the vendor
reports as a protobuf oneof complaint naming neither `prompt` nor "empty".

`stream=true` still works: the canonical reply is complete before it exists, so
the SSE bridge slices it, as it always has.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from adapter.api.common import json_ok
from adapter.api.frontdoor import canonical_to_chat, messages_to_canonical
from adapter.api.images import validate_images_body
from adapter.api.pipeline import adapt
from adapter.utils.sse import stream_chat_as_sse


async def chat_handler(request: Request) -> Response:
    #: The client's own fields, kept for the two the entry level still needs:
    #: `stream` (decides SSE here) and `model` (labels the reply). Neither is
    #: folded into the canonical body -- a script that forwards the client body
    #: verbatim would hand both to the vendor, and `stream` is this route's
    #: decision to make anyway.
    original: dict = {}

    def prepare(body: dict) -> None:
        """Chat body -> canonical body, in place.

        Replacement rather than enrichment, because `adapt` keeps using the dict
        it parsed. Doing it here rather than before the call also keeps the
        admission check and the body-size limit ahead of the work: an
        unauthorised caller must not reach the folding at all.

        The folding runs the canonical validator afterwards rather than growing
        checks of its own, so this door and /v1/images/generations accept and
        reject exactly the same requests -- the same reason /v1/images/edits
        reuses it.
        """
        original.update(body)
        canonical = messages_to_canonical(original)
        validate_images_body(canonical)
        body.clear()
        body.update(canonical)

    outcome = await adapt(request, "images", prepare)
    payload = canonical_to_chat(outcome.payload, original.get("model"))

    if original.get("stream"):
        return StreamingResponse(
            stream_chat_as_sse(payload if isinstance(payload, dict) else {}),
            media_type="text/event-stream",
            headers=outcome.headers,
        )

    data = payload if isinstance(payload, dict) else {}
    data.setdefault("created", int(time.time()))
    data.setdefault("object", "chat.completion")
    return json_ok(data, outcome.request_id, outcome.headers)
