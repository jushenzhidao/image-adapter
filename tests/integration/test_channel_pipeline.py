"""End-to-end checks for the header-driven channel pipeline.

A real local HTTP server stands in for the vendor so the upstream call, the
Authorization passthrough, and both script phases are all exercised.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'desc': payload['prompt'], 'n': payload.get('n', 1)}
    return {'data': [{'url': payload['image']}]}
"""


class _Vendor(BaseHTTPRequestHandler):
    received: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Vendor.received = {
            "body": body,
            "auth": self.headers.get("Authorization"),
        }
        payload = json.dumps({"image": "https://cdn.vendor.test/a.png"}).encode()
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
    yield f"http://127.0.0.1:{server.server_port}/v2/text2img"
    server.shutdown()
    server.server_close()


def test_images_roundtrip(client, channel_headers, vendor):
    headers = channel_headers(SCRIPT, vendor)
    headers["Authorization"] = "Bearer vendor-secret"

    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "a red fox", "n": 2},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/a.png"
    assert _Vendor.received["body"] == {"desc": "a red fox", "n": 2}
    assert _Vendor.received["auth"] == "Bearer vendor-secret"


def test_missing_adapter_key_is_rejected(client, channel_headers, vendor):
    headers = channel_headers(SCRIPT, vendor)
    headers.pop("X-Adapter-Key")

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 401


def test_missing_upstream_url_is_rejected(client, channel_headers, vendor):
    headers = channel_headers(SCRIPT, vendor)
    headers.pop("X-Upstream-Url")

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 400


def test_script_import_is_blocked(client, channel_headers, vendor):
    headers = channel_headers(
        "import os\nasync def transform(ctx, payload, phase):\n    return payload\n",
        vendor,
    )

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "script_security_error"
