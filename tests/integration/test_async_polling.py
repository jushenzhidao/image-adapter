"""Async job polling (X-Async) and script-ref loading, end to end."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.testclient import TestClient

from adapter.settings import Settings
from tests.integration.conftest import ADAPTER_KEY

ASYNC_SCRIPT = """
PHASES = ['request', 'response', 'poll_request', 'poll_response']

async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'prompt': payload['prompt']}
    if phase == 'poll_request':
        ctx.emit(url=ctx.upstream_url.replace('/submit', '/status'))
        return {'job_id': payload['job_id']}
    if phase == 'poll_response':
        if payload.get('state') == 'done':
            return {'done': True, 'payload': payload}
        return {'done': False, 'payload': payload}
    return {'data': [{'url': payload['result_url']}]}
"""


class _JobVendor(BaseHTTPRequestHandler):
    polls = 0
    #: Seconds to stall the *poll* endpoint. The submit path is never delayed:
    #: the bound under test is the one a poll gets, not the one a generation does.
    poll_delay = 0.0
    #: Seconds to stall the submit path, for the test that checks the generation
    #: bound is still the generation-sized one.
    submit_delay = 0.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path.endswith("/submit"):
            if _JobVendor.submit_delay:
                time.sleep(_JobVendor.submit_delay)
            _JobVendor.polls = 0
            body = {"job_id": "job-42", "state": "queued"}
        else:
            if _JobVendor.poll_delay:
                time.sleep(_JobVendor.poll_delay)
            _JobVendor.polls += 1
            if _JobVendor.polls >= 2:
                body = {
                    "job_id": "job-42",
                    "state": "done",
                    "result_url": "https://cdn.vendor.test/done.png",
                }
            else:
                body = {"job_id": "job-42", "state": "running"}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def job_vendor():
    _JobVendor.poll_delay = 0.0
    _JobVendor.submit_delay = 0.0
    server = HTTPServer(("127.0.0.1", 0), _JobVendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/submit"
    server.shutdown()
    server.server_close()


def test_async_job_polls_until_done(client, channel_headers, job_vendor):
    headers = channel_headers(ASYNC_SCRIPT, job_vendor)
    headers["X-Async"] = "poll=0.05,timeout=10"

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "slow art"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/done.png"
    assert _JobVendor.polls >= 2


def test_a_slow_poll_is_bounded_by_the_poll_timeout(
    client, settings, channel_headers, job_vendor
):
    """A poll is a status check, so it gets `poll_request_timeout`, not the
    generation-sized `upstream_timeout`.

    Without that bound one slow poll could overrun the entire job budget:
    `poll_timeout_default` is only checked at the top of the loop, so a poll
    allowed to run for `upstream_timeout` outlives the loop that owns it. The
    error message names the bound it used, which is what lets this be asserted
    without timing anything -- and what would have said "60s" before the change.
    """
    settings.poll_request_timeout = 1.0
    _JobVendor.poll_delay = 2.0
    headers = channel_headers(ASYNC_SCRIPT, job_vendor)
    headers["X-Async"] = "poll=0.05,timeout=10"

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "slow art"}
    )

    assert resp.status_code == 504, resp.text
    assert "within 1s" in resp.text


def test_the_submit_call_still_gets_the_generation_bound(
    client, settings, channel_headers, job_vendor
):
    """The short bound is the poll's alone: submitting a job is a generation
    request and keeps the deployment's `upstream_timeout`."""
    settings.upstream_timeout = 1.0
    _JobVendor.submit_delay = 2.0
    headers = channel_headers(ASYNC_SCRIPT, job_vendor)
    headers["X-Async"] = "poll=0.05,timeout=10"

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "slow art"}
    )

    assert resp.status_code == 504, resp.text
    assert "within 1s" in resp.text


def test_async_without_poll_phases_is_config_error(
    client, channel_headers, job_vendor
):
    plain = (
        "async def transform(ctx, payload, phase):\n"
        "    if phase == 'request':\n"
        "        return payload\n"
        "    return {'data': []}\n"
    )
    headers = channel_headers(plain, job_vendor)
    headers["X-Async"] = "poll=0.05,timeout=5"

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "x"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "channel_config_error"


def test_overlay_ref_wins_over_image_store(tmp_path, monkeypatch):
    """An overlay dir shadows the same ref inside the image, end to end.

    Proves the deploy story: drop a file on the mounted volume and the running
    service serves it instead of the baked-in copy, no rebuild.
    """
    overlay = tmp_path / "overlay" / "volcengine_ark"
    overlay.mkdir(parents=True)
    # Same ref as the in-image script. The engine sanitizes script failures
    # down to the exception type name, so raise a type the real script never
    # raises: seeing it proves the overlay file is what got compiled.
    (overlay / "images@v1.py").write_text(
        "async def transform(ctx, payload, phase):\n    return 1 / 0\n",
        encoding="utf-8",
    )

    from adapter.main import app

    app.state.settings = Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        script_overlay_dirs=str(tmp_path / "overlay"),
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as overlay_client:
            resp = overlay_client.post(
                "/v1/images/generations",
                headers={
                    "X-Adapter-Key": ADAPTER_KEY,
                    "X-Upstream-Url": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                    "X-Script-Ref": "volcengine_ark/images@v1",
                    "Content-Type": "application/json",
                },
                json={"prompt": "cat"},
            )
    finally:
        app.state.settings = None

    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["code"] == "script_runtime_error"
    assert "ZeroDivisionError" in body["message"]


def test_script_ref_from_store(client, channel_headers, settings):
    """volcengine_ark/images@v1 ships in script_store and loads by ref."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        "X-Script-Ref": "volcengine_ark/images@v1",
        "Content-Type": "application/json",
    }
    # No upstream call is made when validation fails first; use a payload that
    # is still refused to stop at validation and prove the ref resolved (a
    # missing ref would produce script_not_found before validation ordering
    # matters). `n` and `response_format` are no longer usable here -- both are
    # normalised rather than refused; a mask with no image still is not.
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "cat", "mask": "data:image/png;base64,AAAA"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "mask"
