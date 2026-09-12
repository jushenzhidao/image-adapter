"""The channel's `image_ref_mode` reaches the wire, through a real request.

tests/unit/test_volcengine_ark_script.py pins what `transform` does once the
option is in hand; this file pins that the option arrives at all. The value
travels as JSON inside the `X-Channel-Options` header the control plane sends,
is parsed by the channel spec, and reaches the script as `ctx.options` -- so an
option can be implemented perfectly and still never arrive. An option that
silently does nothing is the exact defect being fixed here, and asserting it
only at the `transform` level would leave that half untested.

Both servers are real sockets, because two of the assertions are about whether
an outbound request happened at all, and a stand-in would be answering its own
question.
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import pytest
from PIL import Image

from tests.integration.conftest import ADAPTER_KEY

SCRIPT_REF = "volcengine_ark/images@v1"

#: Only the magic bytes matter: this rides through the ark script untouched,
#: and the image host declares its own Content-Type.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"


def _real_png(width: int = 400, height: int = 300) -> bytes:
    """A payload Pillow can open, for the tests where it actually has to."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


REAL_PNG = _real_png()
REAL_DATA_URI = f"data:image/png;base64,{base64.b64encode(REAL_PNG).decode()}"


def _inline_size(value: str) -> tuple[int, int]:
    """The dimensions behind a data URI or a bare base64 payload."""
    payload = base64.b64decode(value.split(";base64,", 1)[-1])
    with Image.open(io.BytesIO(payload)) as img:
        return img.size


class _ImageHost(BaseHTTPRequestHandler):
    """Serves the client's image, and counts how often it is asked for it."""

    hits = 0
    #: Which bytes to serve. The default is the magic-bytes stub, because most
    #: of these tests only care *whether* a fetch happened; the compression
    #: tests install REAL_PNG, since there the picture has to be decodable.
    payload: bytes = PNG

    def do_GET(self):  # noqa: N802 -- the stdlib's spelling
        _ImageHost.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):
        pass


class _Ark(BaseHTTPRequestHandler):
    """Stands in for ARK and keeps the request body it was handed."""

    received: dict = {}

    def do_POST(self):  # noqa: N802 -- the stdlib's spelling
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Ark.received = json.loads(raw)
        body = json.dumps(
            {"created": 1, "data": [{"url": "https://tos.example/o.png"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def image_host():
    _ImageHost.hits = 0
    _ImageHost.payload = PNG
    server, base = _serve(_ImageHost)
    yield f"{base}/holiday.png"
    server.shutdown()
    server.server_close()


@pytest.fixture
def real_image_host():
    """The same host, serving an image the compression policy can act on."""
    _ImageHost.hits = 0
    _ImageHost.payload = REAL_PNG
    server, base = _serve(_ImageHost)
    yield f"{base}/holiday.png"
    server.shutdown()
    server.server_close()


@pytest.fixture
def ark():
    _Ark.received = {}
    server, base = _serve(_Ark)
    yield f"{base}/api/v3/images/generations"
    server.shutdown()
    server.server_close()


def _post(
    client,
    ark_url: str,
    image: str,
    options: dict | None = None,
    script_ref: str = SCRIPT_REF,
):
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": ark_url,
        "X-Script-Ref": script_ref,
        "Content-Type": "application/json",
    }
    if options is not None:
        headers["X-Channel-Options"] = json.dumps(options)
    return client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "replace the circle", "image": image},
    )


def test_the_option_survives_the_header_and_makes_us_fetch_the_image(
    client, ark, image_host
):
    """`data_uri` end to end: one fetch by us, and ARK is given no URL.

    This is the answer to "ARK timed out downloading the client's image": the
    fetch moves to our side of the call, where the budget is not 5 s.
    """
    resp = _post(client, ark, image_host, options={"image_ref_mode": "data_uri"})

    assert resp.status_code == 200, resp.text
    assert _ImageHost.hits == 1
    assert _Ark.received["image"].startswith("data:image/png;base64,")
    assert image_host not in _Ark.received["image"]


def test_without_the_option_ark_is_still_the_one_that_fetches(
    client, ark, image_host
):
    """The default is unchanged, which is what makes promoting v2 safe.

    Zero hits on the image host is the assertion that matters: the URL was
    forwarded verbatim, so the fetch -- and its 5 s cap -- stayed on ARK's side.
    """
    resp = _post(client, ark, image_host)

    assert resp.status_code == 200, resp.text
    assert _ImageHost.hits == 0
    assert _Ark.received["image"] == image_host


# The `ref_*` policy, end to end. A unit test can prove `transform` honours an
# option once it is in hand; only a real request proves the option is in hand,
# which is the half that fails silently when the control plane is involved.


def test_the_ref_option_survives_the_header_and_shrinks_the_reference(client, ark):
    """One request twice, one header changed, two opposite observations.

    The control half is not decoration: it is what proves the difference came
    from the option rather than from the reference having been shrunk anyway.
    """
    resp = _post(
        client,
        ark,
        REAL_DATA_URI,
        options={"image_ref_mode": "data_uri"},
    )
    assert resp.status_code == 200, resp.text
    untouched = _Ark.received["image"]
    assert _inline_size(untouched) == (400, 300)

    resp = _post(
        client,
        ark,
        REAL_DATA_URI,
        options={"image_ref_mode": "data_uri", "ref_max_edge": 64},
    )
    assert resp.status_code == 200, resp.text
    shrunk = _Ark.received["image"]
    assert _inline_size(shrunk) == (64, 48)
    assert len(shrunk) < len(untouched)


def test_a_shrunk_reference_is_what_reaches_object_storage(client, ark, storage):
    """The upload is the compressed picture, and the link points at it.

    `storage.puts` is the only place the bytes that actually left the process
    can be inspected, which is why this test needs a store rather than a fake
    one inside the script.
    """
    resp = _post(
        client,
        ark,
        REAL_DATA_URI,
        options={"ref_max_edge": 64},
    )

    assert resp.status_code == 200, resp.text
    key, content_type, data = storage.puts[0]
    assert content_type == "image/png"
    assert _inline_size(base64.b64encode(data).decode()) == (64, 48)
    assert _Ark.received["image"] == f"https://cdn.test/{key}"


def test_the_policy_never_makes_us_fetch_a_url_in_url_mode(
    client, ark, real_image_host
):
    """The exemption stated in the docstring, observed from outside.

    Zero hits on the image host: the reference was handed over as it arrived,
    so `ref_*` bought nothing here -- deliberately, because fetching it would
    have spent the 5 s budget this mode exists to keep on ARK's side.
    """
    resp = _post(
        client,
        ark,
        real_image_host,
        options={"ref_max_edge": 64},
    )

    assert resp.status_code == 200, resp.text
    assert _ImageHost.hits == 0
    assert _Ark.received["image"] == real_image_host


def test_a_malformed_option_fails_before_any_upstream_call(client, ark):
    """No upstream request at all, which only a real socket can confirm.

    The code is the point of the test: `channel_config_error` sends the
    operator to `X-Channel-Options`, where the typo is.
    """
    resp = _post(
        client,
        ark,
        REAL_DATA_URI,
        options={"ref_fmt": "tiff"},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "channel_config_error"
    assert _Ark.received == {}


# Concurrency, end to end. The unit suite measures the peak through ctx stubs,
# which proves the *script* asked for the fan-out; only a real socket proves the
# fan-out reached the network, since the loader, the pool and the HTTP client all
# sit between those two claims.


class _ConcurrentImageHost(BaseHTTPRequestHandler):
    """Serves the reference, counting how many requests are in flight.

    Threaded, and that is load-bearing: a single-threaded server serialises its
    replies, so the peak would read 1 whatever the adapter did and the test
    would be measuring itself. The delay is what makes one request long enough
    to overlap another at all.
    """

    hits = 0
    in_flight = 0
    peak = 0
    delay = 0.05
    lock = threading.Lock()

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.hits = 0
            cls.in_flight = 0
            cls.peak = 0

    def do_GET(self):  # noqa: N802 -- the stdlib's spelling
        with _ConcurrentImageHost.lock:
            _ConcurrentImageHost.hits += 1
            _ConcurrentImageHost.in_flight += 1
            _ConcurrentImageHost.peak = max(
                _ConcurrentImageHost.peak, _ConcurrentImageHost.in_flight
            )
        try:
            time.sleep(_ConcurrentImageHost.delay)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(REAL_PNG)))
            self.end_headers()
            self.wfile.write(REAL_PNG)
        finally:
            with _ConcurrentImageHost.lock:
                _ConcurrentImageHost.in_flight -= 1

    def log_message(self, *args):
        pass


@pytest.fixture
def concurrent_image_host():
    _ConcurrentImageHost.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConcurrentImageHost)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/holiday.png"
    server.shutdown()
    server.server_close()


def _post_many(client, ark_url, images, script_ref: str = SCRIPT_REF, options=None):
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": ark_url,
        "X-Script-Ref": script_ref,
        "Content-Type": "application/json",
    }
    if options is not None:
        headers["X-Channel-Options"] = json.dumps(options)
    return client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "replace the circle", "image": images},
    )


def test_the_references_are_materialised_concurrently(
    client, ark, concurrent_image_host
):
    """Three references, all three fetched, more than one at a time.

    Every URL is distinct so a fetch cannot be answered from anything cached,
    and the count is asserted alongside the peak: an overlap of one would
    satisfy a peak assertion on a request that only ever fetched one image.
    """
    images = [f"{concurrent_image_host}?i={i}" for i in range(3)]
    resp = _post_many(
        client,
        ark,
        images,
        options={"image_ref_mode": "data_uri"},
    )
    assert resp.status_code == 200, resp.text
    assert _ConcurrentImageHost.hits == 3
    assert _ConcurrentImageHost.peak > 1, (
        f"the references were fetched serially (peak {_ConcurrentImageHost.peak}); "
        "the fan-out "
        "did not reach the network"
    )
    assert len(_Ark.received["image"]) == 3
