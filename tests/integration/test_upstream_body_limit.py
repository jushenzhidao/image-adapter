"""Upstream response bodies are bounded.

Every reply is buffered in full -- one buffer per in-flight request, see
``executor._do_upstream`` -- so an unbounded body is a direct path into worker
memory: 1000 concurrent requests at 8 MB each is already ~8 GB. These tests pin
the ceiling from three sides:

  declared    the vendor advertises an oversized Content-Length, so the reply
              must be refused before a single body byte is read;
  unterminated the vendor declares no length at all (read to EOF), which is
              the path the running total exists to guard;
  under       a reply inside the cap still flows through untouched, so the
              guard cannot silently break the happy path.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapter.settings import Settings

ADAPTER_KEY = "test-adapter-key"

# Small enough for a test to cross cheaply; the production default is 64 MB.
BODY_LIMIT = 4096
OVERSIZE = BODY_LIMIT * 2
FITS = 64

SCRIPT = (
    "async def transform(ctx, payload, phase):\n"
    "    if phase == 'request':\n"
    "        return {'desc': payload['prompt']}\n"
    "    return {'data': [{'url': payload['image']}]}"
)


@pytest.fixture
def settings() -> Settings:
    """Overrides the shared fixture to shrink the cap to something reachable."""
    return Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        allow_inline_script=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        max_upstream_bytes=BODY_LIMIT,
    )


def _body(payload_bytes: int) -> bytes:
    return json.dumps({"image": "x" * payload_bytes}).encode()


class _Vendor(BaseHTTPRequestHandler):
    """Replies with the framing the test asked for."""

    mode = "fits"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        if self.mode == "declared":
            # Headers only. The point of the test is that the declared length
            # alone is enough to refuse the reply.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(OVERSIZE + 1))
            self.end_headers()
            return

        body = _body(OVERSIZE if self.mode == "unterminated" else FITS)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.mode != "unterminated":
            # HTTP/1.0 with no length means "read until the socket closes",
            # which is what makes content_length None on the client side.
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The client refuses mid-read and hangs up. Expected for the
            # unterminated case, and not something this server needs to log.
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    """Starts one vendor per requested mode, tearing all of them down after."""
    started: list[HTTPServer] = []

    def _start(mode: str) -> str:
        handler = type(f"_Vendor_{mode}", (_Vendor,), {"mode": mode})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_port}/v1/gen"

    yield _start

    for server in started:
        server.shutdown()
        server.server_close()


def test_declared_oversize_is_refused_before_reading(client, vendor, channel_headers):
    headers = channel_headers(SCRIPT, vendor("declared"))
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )

    assert resp.status_code == 502, resp.text
    error = resp.json()["error"]
    assert error["code"] == "upstream_body_too_large"
    assert str(OVERSIZE + 1) in error["message"]


def test_unterminated_oversize_is_refused_while_reading(
    client, vendor, channel_headers
):
    headers = channel_headers(SCRIPT, vendor("unterminated"))
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_body_too_large"


def test_body_within_the_cap_still_flows(client, vendor, channel_headers):
    """The guard must not become a functional regression."""
    headers = channel_headers(SCRIPT, vendor("fits"))
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "x" * FITS
