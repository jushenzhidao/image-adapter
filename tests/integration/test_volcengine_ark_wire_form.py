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

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.integration.conftest import ADAPTER_KEY

SCRIPT_REF = "volcengine_ark/images@v2"

#: Only the magic bytes matter: this rides through the ark script untouched,
#: and the image host declares its own Content-Type.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"


class _ImageHost(BaseHTTPRequestHandler):
    """Serves the client's image, and counts how often it is asked for it."""

    hits = 0

    def do_GET(self):  # noqa: N802 -- the stdlib's spelling
        _ImageHost.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        self.wfile.write(PNG)

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


def _post(client, ark_url: str, image: str, options: dict | None = None):
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": ark_url,
        "X-Script-Ref": SCRIPT_REF,
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
