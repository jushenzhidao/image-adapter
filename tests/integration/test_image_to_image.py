"""Image-to-image through /v1/images/generations.

There is no /v1/images/edits route by design: editing is the same request with
an `image` field, so these tests cover the canonical-format claim end to end —
a client image arrives as a URL, a data URI or bare base64, and the script
converts it into whatever the vendor accepts.

A real local HTTP server stands in for the vendor so the upstream body, the
image download, and both script phases are all exercised.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")

# Forwards the image untouched: asserts the adapter does not strip it.
PASSTHROUGH_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        body = {'prompt': payload.get('prompt', '')}
        if payload.get('image'):
            body['image'] = payload['image']
        if payload.get('mask'):
            body['mask'] = payload['mask']
        return body
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""

# Normalises every incoming shape to bare base64, the common vendor contract.
TO_B64_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'image': await ctx.image_b64(payload['image'])}
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""

# Normalises to a data URI, which is what chat-style vendors want.
TO_DATA_URI_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'image': await ctx.image_data_uri(payload['image'])}
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""

# Exercises the stdlib import path that SAFE_BUILTINS used to block.
IMPORT_SCRIPT = """
import base64

async def transform(ctx, payload, phase):
    if phase == 'request':
        raw = await ctx.image_bytes(payload['image'])
        return {'image': base64.b64encode(raw).decode('ascii'), 'size': len(raw)}
    return {'data': [{'b64_json': payload.get('img', '')}]}
"""


class _Vendor(BaseHTTPRequestHandler):
    received: dict = {}

    def do_GET(self):
        # Serves the asset that ctx.image_bytes downloads.
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1X1)))
        self.end_headers()
        self.wfile.write(PNG_1X1)

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Vendor.received = {}
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _post(client, headers, body):
    return client.post("/v1/images/generations", headers=headers, json=body)


# --- the image field survives the adapter ---------------------------------


def test_image_url_reaches_the_upstream(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "make it red", "image": "https://cdn.test/a.png"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == "https://cdn.test/a.png"
    assert resp.json()["data"][0]["b64_json"] == PNG_B64


def test_multi_image_list_reaches_the_upstream(client, channel_headers, vendor):
    refs = ["https://cdn.test/a.png", "https://cdn.test/b.png"]
    resp = _post(
        client,
        channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "blend", "image": refs},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == refs


def test_mask_reaches_the_upstream(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "inpaint", "image": PNG_B64, "mask": PNG_B64},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["mask"] == PNG_B64


def test_text_to_image_still_omits_the_image_field(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert "image" not in _Vendor.received


# --- conversion between the three shapes ----------------------------------


def test_data_uri_is_normalised_to_bare_b64(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(TO_B64_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": f"data:image/png;base64,{PNG_B64}"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_B64


def test_bare_b64_passes_through_unchanged(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(TO_B64_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": PNG_B64},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_B64


def test_url_is_downloaded_and_encoded(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(TO_B64_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": f"{vendor}/assets/sample.png"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_B64


def test_bare_b64_is_promoted_to_a_data_uri(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(TO_DATA_URI_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": PNG_B64},
    )
    assert resp.status_code == 200, resp.text
    # The mime is sniffed from the magic number, not assumed.
    assert _Vendor.received["image"] == f"data:image/png;base64,{PNG_B64}"


def test_script_may_import_allowlisted_stdlib(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(IMPORT_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": f"{vendor}/assets/sample.png"},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["size"] == len(PNG_1X1)
    assert _Vendor.received["image"] == PNG_B64


# --- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "body, param",
    [
        ({"prompt": "x", "image": 123}, "image"),
        ({"prompt": "x", "image": ""}, "image"),
        ({"prompt": "x", "image": ["ok", 5]}, "image"),
        ({"prompt": "x", "mask": PNG_B64}, "mask"),
        ({"image": None}, "prompt"),
        ({"prompt": "   "}, "prompt"),
    ],
)
def test_invalid_bodies_are_rejected(client, channel_headers, vendor, body, param):
    """`image: []` and a malformed `n` used to be listed here.

    Both are now normalised rather than refused -- the key is dropped, or `n`
    becomes one -- so they moved to tests/unit/test_images_validation.py, which
    pins the value each becomes. `{"image": None}` stays: it has no prompt
    either, which is still refused.
    """
    resp = _post(client, channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"), body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == param


def test_image_without_prompt_is_allowed(client, channel_headers, vendor):
    """Upscale/restyle carries no instruction, so prompt is optional here."""
    resp = _post(
        client,
        channel_headers(PASSTHROUGH_SCRIPT, f"{vendor}/v2/img2img"),
        {"image": PNG_B64},
    )
    assert resp.status_code == 200, resp.text
    assert _Vendor.received["image"] == PNG_B64


def test_invalid_base64_is_rejected(client, channel_headers, vendor):
    resp = _post(
        client,
        channel_headers(TO_B64_SCRIPT, f"{vendor}/v2/img2img"),
        {"prompt": "x", "image": "data:image/png;base64,!!!not-base64!!!"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "image"
