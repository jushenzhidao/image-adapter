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
    image             str | list[str]   URL, data URI or bare base64;
                                        null / [] mean "no image" and are dropped
    mask              str               same three shapes
    n                 int               a malformed value falls back to 1
    response_format   "url" | "b64_json"; anything else, null included, means
                                        "unspecified" and is dropped
    size, quality, style, user
    <vendor extras>   passed through untouched

Validation stays deliberately thin. The adapter owns no vendor semantics, so
it rejects only what is malformed for *every* upstream; which fields a given
vendor actually supports is the script's business.

Three things are normalised rather than refused, because an otherwise complete
request should not fail over them: an unset `image` (`null` or `[]`, depending
on the SDK), an unusable `n`, and an unusable `response_format`. Each is the
caller saying nothing useful about a field it does not really care about, and
each has a safe reading -- no image, one image, the channel's own output shape.
That is not a licence to skip checks: a blank string, a missing prompt with no
image, and a `mask` with no image are all still refused.
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
    # A malformed `n` falls back to one image instead of failing the request.
    # `n` is a count the caller may not care about, and refusing an otherwise
    # complete generation over it trades a usable answer for a technicality.
    # `bool` is an int subclass, and n=True is a client bug, not a request for
    # one image, so it takes the same path.
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        body["n"] = 1

    response_format = body.get("response_format")
    # An unusable `response_format` falls back to "unspecified" -- the key is
    # dropped -- instead of failing the request. Every script already reads it
    # that way (`openai/images@v1::_requested_format` and `google` both map
    # anything outside the set to None), so refusing it here made this door the
    # only component that treats junk as fatal while the components that act on
    # the field treat it as silence. An absent key takes this branch too and pops
    # as a no-op; `null` is not a shape either, and several SDKs serialise an
    # unset enum that way.
    #
    # Dropped rather than blanked, for the reason `image` is: `openai/images@v1`
    # returns the canonical body verbatim on its text-to-image path, so a
    # surviving bad value -- or a null -- would reach a vendor that rejects it.
    #
    # The `isinstance` guard is not decoration: a JSON caller can put a list or
    # an object here, and `in` on a frozenset hashes its operand, so a bare
    # membership test turns input that is merely wrong into a TypeError -> 500.
    if not isinstance(response_format, str) or response_format not in RESPONSE_FORMATS:
        body.pop("response_format", None)

    image = body.get("image")
    # `null` and `[]` are the two ways a client says "no image here" -- several
    # SDKs serialise an unset list field as an empty array. Both mean
    # text-to-image, so the key is dropped rather than rejected. Dropped rather
    # than merely tolerated: `openai/images@v1` forwards the canonical body
    # verbatim on its text-to-image path, so a surviving `image: []` would reach
    # a vendor that refuses the argument outright, turning a request this
    # adapter accepted into an upstream error.
    if image is None or image == []:
        body.pop("image", None)
        image = None
    elif isinstance(image, list):
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
