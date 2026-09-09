"""Integration tests for /v1/responses under the channel contract."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

RESPONSES_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'ask': payload['input']}
    return {
        'output': [{
            'type': 'message',
            'content': [{'type': 'output_text', 'text': payload['reply']}],
        }],
    }
"""


class _AgentVendor(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        payload = json.dumps({"reply": "a cat, drawn"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def agent_vendor():
    server = HTTPServer(("127.0.0.1", 0), _AgentVendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/agent"
    server.shutdown()
    server.server_close()


def test_responses_roundtrip(client, channel_headers, agent_vendor):
    resp = client.post(
        "/v1/responses",
        headers=channel_headers(RESPONSES_SCRIPT, agent_vendor),
        json={"input": "draw a cat", "tools": [{"type": "image_generation"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "response"
    assert body["id"].startswith("resp-")
    assert body["output"][0]["content"][0]["text"] == "a cat, drawn"


def test_responses_validation_missing_input(client, channel_headers, agent_vendor):
    resp = client.post(
        "/v1/responses",
        headers=channel_headers(RESPONSES_SCRIPT, agent_vendor),
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "input"
