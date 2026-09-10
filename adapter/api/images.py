"""POST /v1/images/generations — the canonical image request.

This endpoint is the single entry point for every image task: text-to-image,
image-to-image and edits all arrive here. OpenAI's split between generations
and edits is a transport accident (multipart vs JSON) rather than a semantic
one, and the vendors this adapter targets — Volcengine seedream among them —
expose editing through their normal generations endpoint by accepting an
`image` field. /v1/images/edits therefore exists only as a thin multipart
front door (see adapter/api/image_edits.py): it normalises the form into the
body below and rejoins this same pipeline, so scripts implement one contract.

The request shape is therefore a superset of the OpenAI images body:

    prompt            str
    image             str | list[str]   URL, data URI or bare base64
    mask              str               same three shapes
    n, size, quality, response_format, style, user
    <vendor extras>   passed through untouched

Validation stays deliberately thin. The adapter owns no vendor semantics, so
it rejects only what is malformed for *every* upstream; which fields a given
vendor actually supports is the script's business.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import Response

from adapter.api.common import json_ok
from adapter.api.pipeline import adapt
from adapter.errors import InvalidRequestError

RESPONSE_FORMATS = frozenset({"url", "b64_json"})

# Guards the JSON body itself. base64 inflates by 4/3, so this caps the
# encoded form; the decoded byte cap is settings.max_asset_bytes.
MAX_IMAGE_REF_CHARS = 32 * 1024 * 1024


def _check_image_ref(value: object, param: str) -> None:
    """One image reference: a non-empty, plausibly sized string."""
    if not isinstance(value, str):
        raise InvalidRequestError(
            f"'{param}' must be a URL, a data URI or a base64 string", param=param
        )
    if not value.strip():
        raise InvalidRequestError(f"'{param}' must not be empty", param=param)
    if len(value) > MAX_IMAGE_REF_CHARS:
        raise InvalidRequestError(
            f"'{param}' exceeds the {MAX_IMAGE_REF_CHARS} character limit",
            param=param,
        )


def validate_images_body(body: dict) -> None:
    """Shape checks for the canonical images request body.

    Shared with /v1/images/edits: that route only rewrites the transport, so
    running the same checks keeps the two endpoints from drifting apart.
    """
    n = body.get("n", 1)
    # bool is an int subclass, and n=True is a client bug, not a request for
    # one image.
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise InvalidRequestError("'n' must be a positive integer", param="n")

    response_format = body.get("response_format", "url")
    if response_format not in RESPONSE_FORMATS:
        raise InvalidRequestError(
            "'response_format' must be 'url' or 'b64_json'", param="response_format"
        )

    image = body.get("image")
    if image is not None:
        if isinstance(image, list):
            if not image:
                raise InvalidRequestError(
                    "'image' must not be an empty list", param="image"
                )
            for item in image:
                _check_image_ref(item, "image")
        else:
            _check_image_ref(image, "image")

    mask = body.get("mask")
    if mask is not None:
        _check_image_ref(mask, "mask")
        if image is None:
            raise InvalidRequestError("'mask' requires 'image' to be set", param="mask")

    # Some vendors accept a bare image with no instruction (upscale, restyle),
    # so prompt is only mandatory when there is no image to act on.
    if image is None and not str(body.get("prompt") or "").strip():
        raise InvalidRequestError("'prompt' is required", param="prompt")


async def images_handler(request: Request) -> Response:
    outcome = await adapt(request, "images", validate_images_body)

    data = outcome.payload if isinstance(outcome.payload, dict) else {}
    data.setdefault("created", int(time.time()))
    return json_ok(data, outcome.request_id, outcome.headers)
