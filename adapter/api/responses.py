"""POST /v1/responses -- a responses-shaped front door onto the image contract.

Like /v1/chat/completions, this route holds no vendor logic: `/v1/images/
generations` is the canonical contract, the folding and the response wrapping
live in ``adapter/api/frontdoor.py``, and both run after admission so an
unauthorised caller never reaches them.

Two things this door carries on top of the folding:

* the state chain -- `previous_response_id` is loaded before the run and the new
  turn is persisted after it;
* `tools`, which on this door means `image_generation` orchestration and travels
  into the canonical body because scripts already read it.
"""

from __future__ import annotations

import time
import uuid

from starlette.requests import Request
from starlette.responses import Response

from adapter.api.common import json_ok
from adapter.api.frontdoor import canonical_to_response, input_to_canonical
from adapter.api.images import validate_images_body
from adapter.api.pipeline import adapt
from adapter.state_store import StateStore

CTX_PREFIX = "resp_ctx:"


async def responses_handler(request: Request) -> Response:
    store: StateStore = request.app.state.state_store
    #: The client's own fields, kept for the one piece the wrapper still needs:
    #: `model`, which labels the reply and is not a canonical field.
    original: dict = {}

    async def prepare(body: dict) -> None:
        """Responses body -> canonical body, in place; see chat_handler.

        The canonical validator runs on the folded body rather than a private
        copy of the checks, so every door accepts and rejects the same requests.
        """
        original.update(body)
        previous_id = original.get("previous_response_id")
        prior = await store.get(f"{CTX_PREFIX}{previous_id}") if previous_id else None
        canonical = input_to_canonical(original, prior)
        validate_images_body(canonical)
        body.clear()
        body.update(canonical)

    outcome = await adapt(request, "images", prepare)
    payload = canonical_to_response(outcome.payload, original.get("model"))

    data = payload if isinstance(payload, dict) else {}
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
