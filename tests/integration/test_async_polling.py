"""Async job polling (X-Async) and script-ref loading, end to end."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

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

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path.endswith("/submit"):
            _JobVendor.polls = 0
            body = {"job_id": "job-42", "state": "queued"}
        else:
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


def test_script_ref_from_store(client, channel_headers, settings):
    """volcengine_ark/images@v1 ships in script_store and loads by ref."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        "X-Script-Ref": "volcengine_ark/images@v1",
        "Content-Type": "application/json",
    }
    # No upstream call is made when validation fails first; use a bad payload
    # to stop at validation and prove the ref resolved (a missing ref would
    # produce script_not_found before validation ordering matters).
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat", "n": 0}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "n"
