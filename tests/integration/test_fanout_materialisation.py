"""openai/images@v1 through real sockets: does the fan-out reach the network?

Two things have to be shown, and neither can be shown by reading the code:

  * the fan-out is **real** -- the caller's image origin sees concurrent requests
    rather than one at a time, in both directions (the references on the way in,
    the reply items on the way back);
  * the contract is **unchanged** -- the same part names and order, and the same
    N=1 behaviour, which is the case a fan-out must leave alone.

Real sockets on both sides, because "did two requests overlap in time" is not a
question a stub can answer. The image source is a ``ThreadingHTTPServer``: a
plain ``HTTPServer`` serves one request at a time, so a concurrent client would
look serial and every peak assertion here would be vacuous.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ADAPTER_KEY = "test-adapter-key"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")

#: Long enough that a serial loop cannot overlap by accident, short enough to
#: keep the suite quick.
DELAY = 0.05

GEN_PATH = "/v1/images/generations"


class _ImageSource(BaseHTTPRequestHandler):
    """Serves one PNG per path, slowly, and remembers how many were in flight."""

    lock = threading.Lock()
    in_flight = 0
    peak = 0
    hits: list[str] = []

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.in_flight = 0
            cls.peak = 0
            cls.hits = []

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.in_flight += 1
            cls.peak = max(cls.peak, cls.in_flight)
            cls.hits.append(self.path)
        try:
            time.sleep(DELAY)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
        finally:
            with cls.lock:
                cls.in_flight -= 1

    def log_message(self, *args):
        pass


class _Vendor(BaseHTTPRequestHandler):
    """Answers any generation or edit with N links into the image source.

    The links carry a per-request serial so that no two requests share one: the
    adapter caches a downloaded URL for its own TTL, and a cached second request
    would make the concurrency comparison below measure nothing.
    """

    source_base = ""
    item_count = 1
    bodies: list[bytes] = []

    def do_POST(self):
        cls = type(self)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        cls.bodies.append(body)
        serial = len(cls.bodies)
        items = [
            {"url": f"{cls.source_base}/out{serial}-{i}.png"}
            for i in range(cls.item_count)
        ]
        raw = json.dumps({"created": 1, "data": items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def image_source():
    """A recording image origin.

    The counters are class state, so they are cleared here rather than assumed
    clean: a peak leaked from an earlier test would make a later serial
    assertion pass for the wrong reason.
    """
    _ImageSource.reset()
    server, base = _serve(_ImageSource)
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture
def vendor(image_source):
    _Vendor.source_base = image_source
    _Vendor.item_count = 1
    _Vendor.bodies = []
    server, base = _serve(_Vendor)
    yield base
    server.shutdown()
    server.server_close()


def _headers(vendor: str, script_ref: str) -> dict[str, str]:
    return {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": f"{vendor}{GEN_PATH}",
        "X-Script-Ref": script_ref,
        "Content-Type": "application/json",
    }


def _edit(client, vendor, image_source, script_ref: str, count: int):
    """One multi-reference edit, as the caller would send it."""
    refs = [f"{image_source}/ref{i}.png" for i in range(count)]
    return client.post(
        "/v1/images/generations",
        headers=_headers(vendor, script_ref),
        json={"prompt": "x", "image": refs},
    )


def _filenames_in_order(body: bytes) -> list[str]:
    """The image part filenames, in the order they appear in the body."""
    return [
        chunk.split(b'"', 1)[0].decode()
        for chunk in body.split(b'filename="')[1:]
    ]


# --- the inbound fan-out: the client's references -------------------------


def test_references_are_fetched_concurrently(client, vendor, image_source):
    """The fan-out has to reach the network, not just the script.

    The hit count is asserted alongside the peak: an overlap of one would satisfy
    a peak assertion on a request that only ever fetched one reference.
    """
    response = _edit(client, vendor, image_source, "openai/images@v1", 3)

    assert response.status_code == 200
    assert len(_ImageSource.hits) == 3
    assert _ImageSource.peak > 1, (
        f"the references were fetched serially (peak {_ImageSource.peak}); "
        "ctx.fanout is not reaching the reference downloads"
    )


def test_a_single_reference_is_left_serial(client, vendor, image_source):
    """N=1 is the common case and the one every fan-out site must leave alone."""
    response = _edit(client, vendor, image_source, "openai/images@v1", 1)

    assert response.status_code == 200
    assert _ImageSource.peak == 1
    assert len(_ImageSource.hits) == 1


def test_the_parts_reach_the_vendor_in_the_same_order(client, vendor, image_source):
    """The caller's contract: same part names, same order.

    Order is part of it because a fan-out that reassembled the batch in
    completion order would reorder the references, and a reference's position is
    meaningful to the model.
    """
    assert _edit(client, vendor, image_source, "openai/images@v1", 3).status_code == 200
    body = _Vendor.bodies[0]

    assert _filenames_in_order(body) == ["image0.png", "image1.png", "image2.png"]
    assert b'name="image[]"' in body


# --- the outbound fan-out, which is the other half ------------------------


def test_reply_items_are_converted_concurrently(client, vendor, image_source):
    """The way back: N reply items each need their picture fetched, and the
    caller gets one b64 per item however many were in flight."""
    _Vendor.item_count = 3
    response = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, "openai/images@v1"),
        json={"prompt": "x", "response_format": "b64_json"},
    )

    assert response.status_code == 200
    assert _ImageSource.peak > 1, "the reply-item conversions did not overlap"
    data = response.json()["data"]
    assert len(data) == 3
    assert all(item["b64_json"] == PNG_B64 for item in data)
