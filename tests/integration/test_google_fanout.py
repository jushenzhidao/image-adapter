"""google/images@v1 through real sockets: does the fan-out reach the network?

Two questions, both of which a unit test cannot answer. First, whether the
concurrency is real end to end -- the script asks `ctx.fanout`, and the pool,
the client and the loader all sit between that call and an overlapping pair of
requests, so the peak is measured at the image host rather than at the stub.
Second, whether the *video* of the request is unchanged: google's inbound change
is a hoist rather than an in-place rewrite, and the risk that creates is not "the
fan-out is missing" but "the fan-out changed which image got inlined". The
budget test is that assertion, and it runs in ``auto`` mode -- the only mode
where the budget decides anything.

Real sockets on both sides. The image source is a ``ThreadingHTTPServer``: a
plain ``HTTPServer`` serves one request at a time, so a concurrent client would
look serial and every peak assertion here would read 1 for a reason unrelated to
the adapter.
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

#: Enough that a serial loop cannot overlap by accident.
DELAY = 0.05

MODEL = "nano-banana-pro"
UPSTREAM_PATH = "/v1beta/models/gemini-2.5-flash-image:generateContent"


class _ImageSource(BaseHTTPRequestHandler):
    """One PNG per path, slowly, remembering how many were in flight."""

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
    """Answers generateContent with however many images the test asks for."""

    image_count = 1
    bodies: list[bytes] = []

    def do_POST(self):
        cls = type(self)
        cls.bodies.append(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        parts = [
            {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
            for _ in range(cls.image_count)
        ]
        raw = json.dumps(
            {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}},
                ],
                "usageMetadata": {"totalTokenCount": 3},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class _CountingStore:
    """A fake object store that is slow, and counts overlap."""

    name = "counting"
    lock = threading.Lock()
    in_flight = 0
    peak = 0

    async def put(self, data: bytes, *, key: str, content_type: str):
        cls = type(self)
        import asyncio

        from adapter.storage import StoredObject

        with cls.lock:
            cls.in_flight += 1
            cls.peak = max(cls.peak, cls.in_flight)
        try:
            await asyncio.sleep(DELAY)
            return StoredObject(
                url=f"https://cdn.test/{key}", key=key, visibility="public"
            )
        finally:
            with cls.lock:
                cls.in_flight -= 1

    async def ping(self) -> bool:
        return True


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def image_source():
    _ImageSource.reset()
    server, base = _serve(_ImageSource)
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture
def vendor(image_source):
    _Vendor.image_count = 1
    _Vendor.bodies = []
    server, base = _serve(_Vendor)
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture
def store(client):
    """Installs the counting store for one test, the way conftest's does."""
    from adapter.main import app

    _CountingStore.in_flight = 0
    _CountingStore.peak = 0
    fake = _CountingStore()
    app.state.storage = fake
    yield fake
    app.state.storage = None


def _headers(vendor: str, script_ref: str, options: dict | None = None) -> dict[str, str]:
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": f"{vendor}{UPSTREAM_PATH}",
        "X-Script-Ref": script_ref,
        "Content-Type": "application/json",
    }
    if options is not None:
        headers["X-Channel-Options"] = json.dumps(options)
    return headers


def _edit(client, vendor, image_source, script_ref: str, count: int, **options):
    refs = [f"{image_source}/ref{i}.png" for i in range(count)]
    return client.post(
        "/v1/images/generations",
        headers=_headers(vendor, script_ref, options or None),
        json={"model": MODEL, "prompt": "x", "image": refs},
    )


def _body_shape(body: dict) -> list[str]:
    """The part sequence of a vendor body, hosts and bytes abstracted away.

    The object key is deliberately unguessable and carries the request id, so a
    stored URL cannot be compared verbatim between runs -- but which reference
    was inlined, which was re-hosted, and in what order is exactly what these
    tests are about.
    """
    return [next(iter(part)) for part in body["contents"][0]["parts"]]


# --- the downloads overlap ------------------------------------------------


def test_client_references_are_fetched_concurrently(client, vendor, image_source):
    """The fan-out has to reach the network, not just the script.

    ``inline`` mode is what makes the adapter fetch at all: by default a client
    URL is handed to Gemini verbatim (``client_url_passthrough``), so there is no
    download to measure.

    The hit count is asserted alongside the peak: an overlap of one would satisfy
    a peak assertion on a request that only ever fetched one reference.
    """
    response = _edit(
        client, vendor, image_source, "google/images@v1", 3, image_ref_mode="inline"
    )

    assert response.status_code == 200, response.text
    assert len(_ImageSource.hits) == 3
    assert _ImageSource.peak > 1, (
        f"the references were fetched serially (peak {_ImageSource.peak}); the "
        "hoisted prefetch is not reaching the downloads"
    )


def test_a_single_reference_is_left_serial(client, vendor, image_source):
    """N=1 is the case every fan-out site must leave alone."""
    response = _edit(
        client, vendor, image_source, "google/images@v1", 1, image_ref_mode="inline"
    )

    assert response.status_code == 200, response.text
    assert _ImageSource.peak == 1


# --- and the body it produces is the one the budget decided ---------------


def test_inlining_a_batch_produces_one_inline_part_per_reference(
    client, vendor, image_source
):
    """``inline`` mode is unconditional: every reference is inlined whatever its
    size, so this isolates ordering and encoding from the budget arithmetic."""
    response = _edit(
        client, vendor, image_source, "google/images@v1", 3, image_ref_mode="inline"
    )

    assert response.status_code == 200, response.text
    body = json.loads(_Vendor.bodies[0])
    assert _body_shape(body) == ["text", "inlineData", "inlineData", "inlineData"]
    inlined = [p for p in body["contents"][0]["parts"] if "inlineData" in p]
    assert {p["inlineData"]["data"] for p in inlined} == {PNG_B64}


def test_the_budget_still_decides_in_input_order(
    client, vendor, image_source, store
):
    """The load-bearing assertion for this script.

    ``auto`` mode is the only mode where the inline budget decides anything, and
    that budget is read-then-written as the loop walks the references. If the
    hoisted prefetch also parallelised the *decisions*, which image ends up
    inlined would depend on which download finished first -- the same request
    would produce different bodies run to run, and the
    ``inline_total_max_bytes`` guard would stop being a guard.

    The budget is set to fit exactly one 68-byte image, so the split is forced
    and the second and third must be re-hosted -- in that order, which is what
    the part sequence pins. ``client_url_passthrough`` is turned off because
    otherwise the references never reach us at all, and the store is installed
    because a reference that has to be hosted with no storage fails loudly by
    design (413) rather than silently inlining.
    """
    options = {
        "image_ref_mode": "auto",
        "client_url_passthrough": False,
        "inline_max_bytes": 4096,
        "inline_total_max_bytes": 100,
    }

    response = _edit(client, vendor, image_source, "google/images@v1", 3, **options)

    assert response.status_code == 200, response.text
    parts = json.loads(_Vendor.bodies[0])["contents"][0]["parts"]
    assert _body_shape(json.loads(_Vendor.bodies[0])) == [
        "text",
        "inlineData",
        "fileData",
        "fileData",
    ]
    assert sum("inlineData" in p for p in parts) == 1
    assert sum("fileData" in p for p in parts) == 2


# --- the reply side: uploads, concurrently --------------------------------


def test_hosted_replies_are_uploaded_concurrently(client, vendor, image_source, store):
    _Vendor.image_count = 3
    response = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, "google/images@v1"),
        json={"model": MODEL, "prompt": "x", "response_format": "url"},
    )

    assert response.status_code == 200, response.text
    assert _CountingStore.peak > 1, "the reply uploads did not overlap"
    data = response.json()["data"]
    assert len(data) == 3

    assert all(item["url"].startswith("https://cdn.test/") for item in data)
