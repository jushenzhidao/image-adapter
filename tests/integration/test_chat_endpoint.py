"""Integration tests for /v1/chat/completions under the channel contract."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

CHAT_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'q': payload['prompt']}
    return {
        'id': 'chatcmpl-test1',
        'model': 'vendor-chat',
        'choices': [{
            'index': 0,
            'message': {'role': 'assistant', 'content': payload['answer']},
            'finish_reason': 'stop',
        }],
    }
"""


class _ChatVendor(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        payload = json.dumps({"answer": "hello from vendor"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def chat_vendor():
    server = HTTPServer(("127.0.0.1", 0), _ChatVendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/chat"
    server.shutdown()
    server.server_close()


def test_chat_non_streaming(client, channel_headers, chat_vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=channel_headers(CHAT_SCRIPT, chat_vendor),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from vendor"


def test_chat_streaming_bridge(client, channel_headers, chat_vendor):
    """stream=true slices the complete upstream reply into SSE chunks."""
    resp = client.post(
        "/v1/chat/completions",
        headers=channel_headers(CHAT_SCRIPT, chat_vendor),
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.text.rstrip().endswith("data: [DONE]")
    assert "hello fr" in resp.text


def test_chat_validation_missing_messages(client, channel_headers, chat_vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=channel_headers(CHAT_SCRIPT, chat_vendor),
        json={},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "messages"
