"""混元渠道的端到端：真 HTTP 假上游 + 真适配器 + 真脚本（`tencent_hunyuan/images@v1`）。

单测（`tests/unit/test_tencent_hunyuan_script.py`）钉的是脚本的翻译判据；这里钉的是
**装进管线之后**仍然成立的东西：请求真的以 chat 形状发出，`choices[0].delta.image.url`
真的被取出来，上游错误真的原样透传（2026-10-01 线上用失效 key 实测到 401 的 message
被带出，这条就是那次现象的钉子）。

零消耗：上游是本地假服务器，不发真实生成请求。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPT_REF = "tencent_hunyuan/images@v1"


class _Hunyuan(BaseHTTPRequestHandler):
    received: dict = {}
    status: int = 200
    response: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _Hunyuan.received = {
            "body": json.loads(self.rfile.read(length) or b"{}"),
            "auth": self.headers.get("Authorization"),
            "path": self.path,
        }
        payload = json.dumps(_Hunyuan.response, ensure_ascii=False).encode()
        self.send_response(_Hunyuan.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    _Hunyuan.status = 200
    _Hunyuan.response = {
        "choices": [{"delta": {"image": {"url": "https://cos.vendor.test/h.png"}}}]
    }
    server = HTTPServer(("127.0.0.1", 0), _Hunyuan)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1/wand/hunyuan-image/v35-generation"
    server.shutdown()
    server.server_close()


def _headers(vendor: str) -> dict[str, str]:
    return {
        "X-Upstream-Url": vendor,
        "X-Script-Ref": SCRIPT_REF,
        "Authorization": "Bearer tokenhub-secret",
    }


def test_t2i_roundtrip_sends_the_chat_shape_and_reads_delta_image_url(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "一只晒太阳的橘猫", "size": "1024x1024", "seed": 42},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cos.vendor.test/h.png"

    seen = _Hunyuan.received
    assert seen["auth"] == "Bearer tokenhub-secret"        # key 是上游透传的
    body = seen["body"]
    # chat 形状：单条 user 消息，文本 part 在后（图在前，见 docs/11 §字段映射）
    content = body["messages"][-1]["content"]
    assert body["messages"][-1]["role"] == "user"
    assert content[-1]["type"] == "text" and "橘猫" in content[-1]["text"]
    # size 两侧同拼写、原样转发；vendor 字段随 payload 带上
    assert body["size"] == "1024x1024"
    assert body["seed"] == 42


def test_upstream_error_message_is_passed_through(client, vendor):
    """上游 401 的原文必须出现在适配器的错误体里 —— 否则运营方只看到一个光秃秃的 502。"""
    _Hunyuan.status = 401
    _Hunyuan.response = {
        "error": {
            "message": "The API Key does not exist or signature verification failed.",
        }
    }

    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "let me in"},
    )

    assert resp.status_code >= 400, resp.text
    assert "API Key does not exist" in resp.text
