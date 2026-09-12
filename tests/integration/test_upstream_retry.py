"""The retry, through a real request: the vendor is called twice.

The contract is two lines long: the engine offers the script a second request
phase, and sends a second upstream call only if the request that came back is not
the one that just failed. "How many times was the vendor called" is a question
only a real socket can answer, so every case here runs one.

The script is supplied inline (`X-Script-64`, because a script has newlines and a
header does not) rather than taken from `script_store`, so the engine feature is
not tested through one channel's particular script. No channel option is involved
anywhere: the engine offers unconditionally, and the judgement about the error text
belongs to the script.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.integration.conftest import ADAPTER_KEY

#: The wording a script recognises. Only the substring matters.
MARKER = "Timeout while downloading url="

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"


class _Upstream(BaseHTTPRequestHandler):
    """Stands in for the vendor, refuses the first N calls, keeps every body."""

    calls = 0
    bodies: list = []
    #: How many of the first calls are refused. 0 means it never fails.
    fail_times = 0
    message = ""
    #: The status those refusals carry. 400 is the one the engine answers;
    #: a 5xx is deliberately not, and that difference is worth a test.
    status = 400

    def do_POST(self):  # noqa: N802 -- the stdlib's spelling
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Upstream.calls += 1
        _Upstream.bodies.append(json.loads(raw))
        if _Upstream.calls <= _Upstream.fail_times:
            self._reply(_Upstream.status, {"error": {"message": _Upstream.message}})
        else:
            self._reply(
                200, {"created": 1, "data": [{"url": "https://tos.example/o.png"}]}
            )

    def _reply(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _ImageHost(BaseHTTPRequestHandler):
    """Serves the client's reference image and counts how often we fetch it."""

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


def _serve(handler) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def upstream():
    _Upstream.calls = 0
    _Upstream.bodies = []
    _Upstream.fail_times = 0
    _Upstream.message = ""
    _Upstream.status = 400
    server, base = _serve(_Upstream)
    yield f"{base}/v1/generations"
    server.shutdown()
    server.server_close()


@pytest.fixture
def image_host():
    _ImageHost.hits = 0
    server, base = _serve(_ImageHost)
    yield f"{base}/holiday.png"
    server.shutdown()
    server.server_close()


def _script(answer: bool) -> str:
    """The two shapes a script can have.

    `answer=True` inspects the error and inlines only what it recognises, the way
    the ark script does; `answer=False` has no branch at all and rebuilds the
    request it just made. The second is what the engine's unchanged-body guard is
    for.
    """
    branch = (
        "        if ctx.upstream_error and "
        f"{MARKER!r} in ctx.upstream_error.get('message', ''):\n"
        "            image = await ctx.image_data_uri(image)\n"
        if answer
        else ""
    )
    return (
        "async def transform(ctx, payload, phase):\n"
        "    if phase == 'request':\n"
        "        image = payload.get('image')\n"
        + branch
        + "        return {'prompt': payload.get('prompt', ''), 'image': image}\n"
        "    return {'created': 1, 'data': payload.get('data', [])}\n"
    )


def _post(client, upstream_url: str, image: str, script: str):
    encoded = base64.b64encode(script.encode()).decode()
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": upstream_url,
        "X-Script-64": encoded,
        "Content-Type": "application/json",
    }
    return client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "replace the circle", "image": image},
    )


def test_a_recognised_failure_gets_exactly_one_more_attempt(client, upstream, image_host):
    """The incident, answered: the first attempt hands the vendor a URL it
    cannot fetch, the second hands it the bytes instead. No option is set --
    this is what an upgraded script does on its own."""
    _Upstream.fail_times = 1
    _Upstream.message = f"The parameter `image` are not valid: {MARKER}{image_host}"

    resp = _post(client, upstream, image_host, _script(answer=True))

    assert resp.status_code == 200, resp.text
    assert _Upstream.calls == 2
    assert _Upstream.bodies[0]["image"] == image_host
    assert _Upstream.bodies[1]["image"].startswith("data:image/png;base64,")
    # We paid the local download once, on the retry, and the vendor's second
    # body carries no URL for it to fetch.
    assert _ImageHost.hits == 1


def test_a_second_failure_is_reported_without_a_third_call(client, upstream, image_host):
    """Never a loop. The retry is worth one extra call, not a chain of them."""
    _Upstream.fail_times = 99
    _Upstream.message = f"still failing: {MARKER}{image_host}"

    resp = _post(client, upstream, image_host, _script(answer=True))

    assert resp.status_code >= 400
    assert _Upstream.calls == 2


def test_a_server_error_is_not_offered_at_all(client, upstream, image_host):
    """A 5xx does not say the generation never happened, so the engine does not
    even ask the script -- re-sending on an unknown outcome is how a request gets
    billed twice. The script here *would* have answered; it is never consulted."""
    _Upstream.fail_times = 1
    _Upstream.status = 503
    _Upstream.message = f"The parameter `image` are not valid: {MARKER}{image_host}"

    resp = _post(client, upstream, image_host, _script(answer=True))

    assert resp.status_code >= 400
    assert _Upstream.calls == 1
    assert _ImageHost.hits == 0


def test_a_failure_the_script_does_not_recognise_is_not_retried(
    client, upstream, image_host
):
    """A content refusal and a fetch timeout arrive as the same 400. The script
    declines this one, so the rebuilt request is identical and nothing is sent
    -- which is the judgement that used to live in a config string."""
    _Upstream.fail_times = 1
    _Upstream.message = "The parameter `prompt` is not valid"

    resp = _post(client, upstream, image_host, _script(answer=True))

    assert resp.status_code >= 400
    assert _Upstream.calls == 1
    assert _ImageHost.hits == 0


def test_a_script_with_no_answer_costs_nothing(client, upstream, image_host):
    """The guard that makes the offer safe for every script, including ones
    pinned to a version from before the mechanism existed."""
    _Upstream.fail_times = 1
    _Upstream.message = f"The parameter `image` are not valid: {MARKER}{image_host}"

    resp = _post(client, upstream, image_host, _script(answer=False))

    assert resp.status_code >= 400
    assert _Upstream.calls == 1
    assert _ImageHost.hits == 0
