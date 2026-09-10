"""POST /v1/images/edits — OpenAI's multipart spelling of an image request.

This route owns no adaptation logic. OpenAI splits editing off from
/v1/images/generations for transport reasons (multipart file upload vs JSON),
not semantic ones, so the only job here is to normalise the form into the
canonical images body and hand it to the same pipeline:

    multipart/form-data          canonical JSON
    image=@a.png (file)   ->     image: "<data URI>"
    image[]=@a,@b         ->     image: ["<data URI>", ...]
    mask=@m.png           ->     mask:  "<data URI>"
    prompt=redraw the sky ->     prompt: "redraw the sky"
    n=2                   ->     n: 2            (int)
    <vendor extras>       ->     passed through as text

Uploads become data URIs rather than bare base64 so the mime type survives:
ctx.image_url() and friends can then serve any upstream shape without
re-sniffing. Validation is deliberately not duplicated — the normalised body
goes through the same validate_images_body() as the JSON route, so both
endpoints accept and reject exactly the same requests.
"""

from __future__ import annotations

import base64
import time

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import Response

from adapter.api.common import json_ok
from adapter.api.images import validate_images_body
from adapter.api.pipeline import adapt
from adapter.errors import InvalidRequestError

# Form fields OpenAI defines as integers; everything else stays a string so
# vendor extras survive untouched.
INT_FIELDS = frozenset({"n"})

# Fields that carry an uploaded file.
FILE_FIELDS = frozenset({"image", "mask"})

MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _sniff_mime(data: bytes, declared: str | None) -> str:
    """Trust the bytes over the client's content type.

    Browsers and SDKs routinely send application/octet-stream for a PNG, and
    some vendors reject a payload whose declared type does not match, so the
    magic number wins whenever it is recognised.
    """
    for magic, mime in MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if declared and declared.startswith("image/"):
        return declared
    return "application/octet-stream"


async def _read_upload(upload: UploadFile, param: str, max_bytes: int) -> str:
    """One uploaded file -> data URI, size-capped."""
    data = await upload.read()
    if not data:
        raise InvalidRequestError(f"'{param}' must not be empty", param=param)
    if len(data) > max_bytes:
        raise InvalidRequestError(
            f"'{param}' exceeds the {max_bytes} byte limit", param=param
        )
    mime = _sniff_mime(data, upload.content_type)
    if mime == "application/octet-stream":
        raise InvalidRequestError(
            f"'{param}' is not a recognised image format", param=param
        )
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _coerce_int(name: str, raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise InvalidRequestError(
            f"'{name}' must be an integer", param=name
        ) from None


async def normalise_edits_form(request: Request, max_bytes: int) -> dict:
    """multipart/form-data -> the canonical /v1/images/generations body."""
    try:
        form = await request.form(max_files=64, max_fields=128)
    except Exception:
        raise InvalidRequestError(
            "Request body must be valid multipart/form-data"
        ) from None

    try:
        body: dict = {}
        for key in form.keys():
            # OpenAI's SDKs send repeated `image` (or `image[]`) parts for a
            # multi-reference edit; getlist keeps every one of them.
            values = form.getlist(key)
            field = key[:-2] if key.endswith("[]") else key

            if field in FILE_FIELDS:
                refs = [
                    await _read_upload(v, field, max_bytes)
                    for v in values
                    if isinstance(v, UploadFile)
                ]
                # A string in a file field is still a usable reference: some
                # clients post a URL where the spec wants an upload.
                refs += [v.strip() for v in values if isinstance(v, str) and v.strip()]
                if not refs:
                    raise InvalidRequestError(
                        f"'{field}' must be a file or a reference string", param=field
                    )
                # mask is single-valued upstream; image collapses to a scalar
                # when there is one reference so scripts see the same shape
                # the JSON route produces.
                body[field] = refs if field == "image" and len(refs) > 1 else refs[0]
                continue

            value = values[-1]
            if isinstance(value, UploadFile):
                raise InvalidRequestError(
                    f"'{field}' must be a text field, not a file", param=field
                )
            body[field] = _coerce_int(field, value) if field in INT_FIELDS else value
    finally:
        await form.close()

    return body


async def image_edits_handler(request: Request) -> Response:
    settings = request.app.state.settings
    body = await normalise_edits_form(request, settings.max_asset_bytes)
    outcome = await adapt(request, "images", validate_images_body, body=body)

    data = outcome.payload if isinstance(outcome.payload, dict) else {}
    data.setdefault("created", int(time.time()))
    return json_ok(data, outcome.request_id, outcome.headers)
