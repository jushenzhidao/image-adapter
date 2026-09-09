"""End to end: multi-stage cascade (AC-22..AC-31).

A real local upstream stands in for a vendor so the per-stage calls, the stage
artefacts and the degraded fallback are all exercised over the wire.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

# One upstream path is a text-to-image, one is a super-resolver. The script
# chains them: generate produces a URL, upscale consumes it and returns a new
# one. Pipeline is client_payload -> generate -> upscale.
STAGED_SCRIPT = """
STAGES = ['generate', 'upscale']

async def transform(ctx, payload, phase):
    if phase == 'request:generate':
        ctx.emit(url=ctx.options['generate_url'])
        return {'prompt': payload['prompt']}
    if phase == 'response:generate':
        return {'image': payload['data'][0]['url']}
    if phase == 'request:upscale':
        ctx.emit(url=ctx.options['upscale_url'])
        return {'image': ctx.stage['generate']['image'], 'scale': 2}
    if phase == 'response:upscale':
        return {'created': 0, 'data': [{'url': payload['output']}]}
    return {}
"""

# Same script but the upscale stage is allowed to fail and fall back to the
# generate artefact. The `degraded` phase reshapes that intermediate artefact
# into a client-facing body.
FALLBACK_SCRIPT = """
PHASES = ['request', 'response', 'degraded']
STAGES = ['generate', 'upscale']
STAGE_FALLBACK = ['upscale']

async def transform(ctx, payload, phase):
    if phase == 'request:generate':
        ctx.emit(url=ctx.options['generate_url'])
        return {'prompt': payload['prompt']}
    if phase == 'response:generate':
        return {'image': payload['data'][0]['url']}
    if phase == 'request:upscale':
        ctx.emit(url=ctx.options['upscale_url'])
        return {'image': ctx.stage['generate']['image'], 'scale': 2}
    if phase == 'response:upscale':
        return {'created': 0, 'data': [{'url': payload['output']}]}
    if phase == 'degraded':
        return {'created': 0, 'data': [{'url': payload['image']}]}
    return {}
"""

# Optional preprocess that skips itself when there is no input image, e.g.
# text-to-image requests running against a script that also serves edits.
SKIP_SCRIPT = """
STAGES = ['preprocess', 'generate']

async def transform(ctx, payload, phase):
    if phase == 'request:preprocess':
        if not payload.get('image'):
            return ctx.SKIP
        ctx.emit(url=ctx.options['generate_url'])
        return {'image': payload['image']}
    if phase == 'response:preprocess':
        return {'image': payload['data'][0]['url']}
    if phase == 'request:generate':
        ctx.emit(url=ctx.options['generate_url'])
        return {'prompt': payload.get('prompt', '')}
    return {'created': 0, 'data': [{'url': payload['data'][0]['url']}]}
"""


class _StageVendor(BaseHTTPRequestHandler):
    """Records every call so tests can assert stage ordering and bodies."""

    calls: list[tuple[str, dict]] = []
    fail_upscale = False

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        path = self.path
        _StageVendor.calls.append((path, body))

        if self.fail_upscale and path == "/upscale":
            payload = json.dumps({"error": {"message": "out of quota"}}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if path == "/upscale":
            result = {"output": "https://cdn.vendor.test/upscaled.png"}
        else:
            result = {"data": [{"url": "https://cdn.vendor.test/generated.png"}]}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def staged_vendor():
    server = HTTPServer(("127.0.0.1", 0), _StageVendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield {
        "base": base,
        "generate": f"{base}/generate",
        "upscale": f"{base}/upscale",
        "preprocess": f"{base}/preprocess",
    }
    server.shutdown()
    server.server_close()


def _staged_headers(channel_headers, script, vendor, **extra):
    headers = channel_headers(script, vendor["generate"])
    headers["X-Stage-Urls"] = (
        f"generate={vendor['generate']},upscale={vendor['upscale']}"
    )
    headers.update(extra)
    return headers


def _options(vendor) -> str:
    return json.dumps(
        {"generate_url": vendor["generate"], "upscale_url": vendor["upscale"]}
    )


def test_stages_run_in_order_and_chain_artefacts(client, channel_headers, staged_vendor):
    _StageVendor.calls.clear()
    headers = _staged_headers(channel_headers, STAGED_SCRIPT, staged_vendor)
    headers["X-Channel-Options"] = _options(staged_vendor)

    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "a red fox"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/upscaled.png"
    # Two stages, two upstream calls, in declaration order.
    assert [p for p, _ in _StageVendor.calls] == ["/generate", "/upscale"]
    # The upscale stage received the generate stage's artefact (AC-26).
    assert _StageVendor.calls[1][1]["image"] == "https://cdn.vendor.test/generated.png"
    assert _StageVendor.calls[1][1]["scale"] == 2
    # A fully successful cascade carries no degraded marker.
    assert "X-Adapter-Degraded" not in resp.headers


def test_degraded_stage_returns_prior_artefact_with_marker(
    client, channel_headers, staged_vendor
):
    _StageVendor.calls.clear()
    _StageVendor.fail_upscale = True
    try:
        headers = _staged_headers(
            channel_headers, FALLBACK_SCRIPT, staged_vendor
        )
        headers["X-Channel-Options"] = _options(staged_vendor)
        resp = client.post(
            "/v1/images/generations",
            headers=headers,
            json={"prompt": "a red fox"},
        )
    finally:
        _StageVendor.fail_upscale = False

    # AC-28: the upstream 500 does not become a client error.
    assert resp.status_code == 200, resp.text
    # The generate artefact is what comes back, not the failed upscale.
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/generated.png"
    assert resp.headers["X-Adapter-Degraded"] == "upscale"
    assert resp.headers.get("X-Adapter-Degraded-Reason")
    # Both stages were attempted.
    assert [p for p, _ in _StageVendor.calls] == ["/generate", "/upscale"]


def test_non_degradable_stage_failure_is_an_error(
    client, channel_headers, staged_vendor
):
    """STAGED_SCRIPT declares no STAGE_FALLBACK, so upscale failing is fatal."""
    _StageVendor.calls.clear()
    _StageVendor.fail_upscale = True
    try:
        headers = _staged_headers(channel_headers, STAGED_SCRIPT, staged_vendor)
        headers["X-Channel-Options"] = _options(staged_vendor)
        resp = client.post(
            "/v1/images/generations",
            headers=headers,
            json={"prompt": "a red fox"},
        )
    finally:
        _StageVendor.fail_upscale = False

    assert resp.status_code >= 400
    assert "X-Adapter-Degraded" not in resp.headers


def test_single_stage_scripts_keep_the_original_path(
    client, channel_headers, staged_vendor
):
    """AC-25: no STAGES means no cascade, no stage headers, no marker."""
    _StageVendor.calls.clear()
    plain = (
        "async def transform(ctx, payload, phase):\n"
        "    if phase == 'request':\n"
        "        return {'prompt': payload['prompt']}\n"
        "    return {'created': 0, 'data': [{'url': payload['data'][0]['url']}]}\n"
    )
    headers = channel_headers(plain, staged_vendor["generate"])
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 200, resp.text
    # Exactly one upstream call, unlike the two-stage cascade above.
    assert len(_StageVendor.calls) == 1
    assert "X-Adapter-Degraded" not in resp.headers


def test_skipped_stage_makes_no_upstream_call(client, channel_headers, staged_vendor):
    """AC-31: ctx.SKIP drops the whole stage, upstream call included."""
    _StageVendor.calls.clear()
    headers = _staged_headers(channel_headers, SKIP_SCRIPT, staged_vendor)
    headers["X-Stage-Urls"] = f"preprocess={staged_vendor['preprocess']},generate={staged_vendor['generate']}"
    headers["X-Channel-Options"] = json.dumps(
        {"generate_url": staged_vendor["generate"]}
    )
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "a cat"},  # no image: preprocess returns ctx.SKIP
    )
    assert resp.status_code == 200, resp.text
    # Only the generate stage reached the vendor.
    assert [p for p, _ in _StageVendor.calls] == ["/generate"]


def test_unknown_stage_in_header_is_rejected(client, channel_headers, staged_vendor):
    headers = _staged_headers(channel_headers, STAGED_SCRIPT, staged_vendor)
    headers["X-Stages"] = "generate,does_not_exist"
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 400


def test_stage_header_without_declared_stages_is_rejected(
    client, channel_headers, staged_vendor
):
    """X-Stages cannot conjure a cascade out of a single-stage script."""
    plain = (
        "async def transform(ctx, payload, phase):\n"
        "    if phase == 'request':\n"
        "        return {'prompt': payload['prompt']}\n"
        "    return {'created': 0, 'data': []}\n"
    )
    headers = channel_headers(plain, staged_vendor["generate"])
    headers["X-Stages"] = "generate,upscale"
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "channel_config_error"


class _SlowVendor(BaseHTTPRequestHandler):
    """Stalls long enough that a small cascade budget must intervene."""

    delay = 3.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(self.delay)
        body = json.dumps(
            {"data": [{"url": "https://cdn.vendor.test/x.png"}], "output": "o"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def slow_vendor():
    server = HTTPServer(("127.0.0.1", 0), _SlowVendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/gen"
    server.shutdown()
    server.server_close()


@pytest.fixture
def tight_budget_client(settings):
    """A client whose cascade budget is smaller than one stage's latency."""
    from starlette.testclient import TestClient

    from adapter.main import app

    settings.stage_budget_default = 2.0
    app.state.settings = settings
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.state.settings = None


def test_budget_exhaustion_returns_504_and_stops_early(
    tight_budget_client, channel_headers, slow_vendor
):
    """AC-27: the shared budget cuts the cascade off rather than letting each
    stage spend its own full timeout."""
    headers = channel_headers(STAGED_SCRIPT, slow_vendor)
    headers["X-Channel-Options"] = json.dumps(
        {"generate_url": slow_vendor, "upscale_url": slow_vendor}
    )

    started = time.monotonic()
    resp = tight_budget_client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "pipeline_budget_exceeded"
    # Cut off near the 2s budget, not after two 3s stages.
    assert elapsed < 4.0
