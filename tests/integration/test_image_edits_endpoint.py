"""/v1/images/edits — multipart in, canonical images body out.

The route owns no adaptation logic: it rewrites OpenAI's multipart spelling
into the /v1/images/generations body and rejoins that pipeline. These tests
therefore assert on what the *script* receives, because that is the contract
the rewrite has to preserve. A real local HTTP server stands in for the vendor
so the upstream body is observed rather than mocked.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")
PNG_DATA_URI = f"data:image/png;base64,{PNG_B64}"

JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDIzM//bAEMBCQkJDAsMGA0NGDIhHCEyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEB"
    "AxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAv/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAA"
    "AAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k="
)

# Echoes the normalised body back so the test can assert on the rewrite.
ECHO_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return dict(payload)
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""

# Proves an uploaded file is a first-class image reference downstream.
TO_B64_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'image': await ctx.image_b64(payload['image'])}
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""


class _Vendor(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Vendor.received = json.loads(self.rfile.read(length) or b"{}")
        payload = json.dumps({"img": PNG_B64}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    server = HTTPServer(("127.0.0.1", 0), _Vendor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Vendor.received = {}
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _headers(channel_headers, script, url):
    # requests sets the multipart Content-Type (with its boundary) itself.
    headers = channel_headers(script, url)
    headers.pop("Content-Type", None)
    return headers


def _post(client, headers, files=None, data=None):
    return client.post("/v1/images/edits", headers=headers, files=files, data=data)


def _png(name="a.png", content_type="image/png"):
    return (name, io.BytesIO(PNG_1X1), content_type)


# --- the multipart -> JSON rewrite ----------------------------------------


def test_upload_becomes_a_data_uri(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "redraw the sky"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_DATA_URI
    assert _Vendor.received["prompt"] == "redraw the sky"


def test_repeated_image_parts_become_a_list(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files=[("image", _png("a.png")), ("image", _png("b.png"))],
        data={"prompt": "blend"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == [PNG_DATA_URI, PNG_DATA_URI]


def test_bracketed_field_name_is_accepted(client, channel_headers, vendor):
    """OpenAI SDKs spell a repeated upload `image[]`."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files=[("image[]", _png("a.png")), ("image[]", _png("b.png"))],
        data={"prompt": "blend"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == [PNG_DATA_URI, PNG_DATA_URI]


def test_single_upload_stays_a_scalar(client, channel_headers, vendor):
    """One reference must look identical to the JSON route's scalar."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files=[("image[]", _png())],
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_DATA_URI


def test_mask_upload_is_normalised(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png(), "mask": _png("m.png")},
        data={"prompt": "inpaint"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["mask"] == PNG_DATA_URI


def test_mime_is_sniffed_not_trusted(client, channel_headers, vendor):
    """SDKs routinely declare octet-stream for a PNG."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": ("a.bin", io.BytesIO(PNG_1X1), "application/octet-stream")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_DATA_URI


def test_jpeg_upload_keeps_its_own_mime(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": ("a.jpg", io.BytesIO(JPEG_1X1), "image/jpeg")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"].startswith("data:image/jpeg;base64,")


def test_n_is_coerced_to_int_and_extras_pass_through(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x", "n": "2", "size": "1024x1024", "guidance_scale": "7.5"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["n"] == 2
    assert _Vendor.received["size"] == "1024x1024"
    # Unknown fields stay text: the adapter owns no vendor semantics.
    assert _Vendor.received["guidance_scale"] == "7.5"


def test_url_in_a_file_field_is_accepted(client, channel_headers, vendor):
    """Some clients post a reference string where the spec wants an upload."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        data={"prompt": "x", "image": "https://cdn.test/a.png"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == "https://cdn.test/a.png"


def test_uploaded_file_is_a_usable_image_reference(client, channel_headers, vendor):
    """The data URI the rewrite produces must work with ctx.image_* helpers."""
    resp = _post(
        client,
        _headers(channel_headers, TO_B64_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_B64
    assert resp.json()["data"][0]["b64_json"] == PNG_B64


def test_response_carries_created_and_trace_headers(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json()["created"], int)
    assert resp.headers["X-Request-Id"]
    assert resp.headers["X-Script-Sha256"]


# --- validation is shared with the JSON route -----------------------------


def test_empty_upload_falls_through_to_text_to_image(client, channel_headers, vendor):
    """An empty file part is "nothing attached", not a malformed reference.

    SDKs submit a zero-length part when the file read came back empty, and HTML
    submits one for an untouched file input. Both mean the caller sent no image
    -- the same statement `image: []` makes on the JSON door -- so the request
    must reach the vendor as text-to-image instead of failing on the transport.
    The key is dropped rather than blanked: a script that forwards the body
    verbatim would otherwise hand the vendor an argument it refuses.
    """
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": ("a.png", io.BytesIO(b""), "image/png")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert "image" not in _Vendor.received
    assert _Vendor.received["prompt"] == "x"


def test_empty_image_text_field_falls_through_too(client, channel_headers, vendor):
    """`image=` arrives as a string part, and means the same as an empty file."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        data={"prompt": "x", "image": ""},
    )
    assert resp.status_code == 200, resp.text
    assert "image" not in _Vendor.received


def test_empty_upload_still_needs_a_prompt(client, channel_headers, vendor):
    """Text-to-image and "nothing at all" stay distinguishable on this door.

    Dropping the empty image makes this a generation request, and a generation
    request without a prompt has nothing to generate from -- the shared
    validator decides that, exactly as it does for `image: []` on the JSON door.
    """
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": ("a.png", io.BytesIO(b""), "image/png")},
        data={"prompt": "  "},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "prompt"


def test_a_usable_reference_survives_an_empty_sibling(client, channel_headers, vendor):
    """Repeated parts are independent: one empty does not lose the other."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files=[
            ("image", ("a.png", io.BytesIO(b""), "image/png")),
            ("image", _png("b.png")),
        ],
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_DATA_URI


def test_empty_mask_upload_is_dropped(client, channel_headers, vendor):
    """`mask: null` is an unset mask on the JSON door; an empty part is the same."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png(), "mask": ("m.png", io.BytesIO(b""), "image/png")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert "mask" not in _Vendor.received
    assert _Vendor.received["image"] == PNG_DATA_URI


def test_non_image_upload_is_rejected(client, channel_headers, vendor):
    """Bytes that are not an image stay a refusal: the part is not empty."""
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": ("a.txt", io.BytesIO(b"not an image"), "text/plain")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "image"


def test_non_integer_n_falls_back_to_one(client, channel_headers, vendor):
    """Unparsable `n` is normalised, not refused.

    The transport must not be stricter than the JSON door for the same request:
    `_coerce_int` hands the raw text on and validate_images_body() replaces it
    with one image.
    """
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x", "n": "two"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["n"] == 1


def test_shared_validator_still_applies(client, channel_headers, vendor):
    """The normalisation is the shared validator's, not this door's own.

    `response_format=webp` is unusable, so validate_images_body() drops the key
    before any script sees it -- this door must produce the canonical body the
    JSON door produces. Asserted on what the *vendor* received, because that is
    the contract the rewrite has to preserve.
    """
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x", "response_format": "webp"},
    )
    assert resp.status_code == 200, resp.text
    assert "response_format" not in _Vendor.received


def test_an_unset_response_format_stays_unspecified(client, channel_headers, vendor):
    """`response_format=` is how a form spells null, and null means "unset".

    The whole field is dropped rather than forwarded blank: the channel's own
    default shape is the answer, exactly as when the field is absent.
    """
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png()},
        data={"prompt": "x", "response_format": ""},
    )
    assert resp.status_code == 200, resp.text
    assert "response_format" not in _Vendor.received


def test_mask_without_image_is_rejected(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"mask": _png("m.png")},
        data={"prompt": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "mask"


def test_no_image_and_no_prompt_is_rejected(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        data={"prompt": "   "},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "prompt"


def test_prompt_field_sent_as_a_file_is_rejected(client, channel_headers, vendor):
    resp = _post(
        client,
        _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img"),
        files={"image": _png(), "prompt": ("p.txt", io.BytesIO(b"x"), "text/plain")},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "prompt"


def test_non_multipart_body_is_rejected(client, channel_headers, vendor):
    headers = channel_headers(ECHO_SCRIPT, f"{vendor}/v2/img2img")
    resp = client.post("/v1/images/edits", headers=headers, json={"prompt": "x"})
    assert resp.status_code == 400, resp.text


def test_admission_still_guards_the_route(client, channel_headers, vendor):
    headers = _headers(channel_headers, ECHO_SCRIPT, f"{vendor}/v2/img2img")
    headers.pop("X-Adapter-Key")
    resp = _post(client, headers, files={"image": _png()}, data={"prompt": "x"})
    assert resp.status_code == 401, resp.text
