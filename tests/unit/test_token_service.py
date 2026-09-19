"""tools/token_service.py：SOCKS5 握手、token 池语义、HTTP 面。

**TLS 那一跳不在这里测**：它需要自签证书才能进单测，而真实通路已经在实机上验过
（经代理取到出口 IP、经代理登录被墙账号拿到 209 的 token）。这里钉的是我手写的那段
握手字节与池子的语义 —— 两处都容易写错、且错了都不报错。
"""

from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "token_service", Path(__file__).resolve().parents[2] / "tools" / "token_service.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)

PROXY_URL = "socks5h://pooluser:poolpass@127.0.0.1:1"


# ------------------------------------------------------- 假 SOCKS5 服务端

class FakeSocks(BaseHTTPRequestHandler):
    """只做握手，握手完就把 socket 交给测试（不发 TLS）。"""

    seen: list[bytes] = []
    lock = threading.Lock()

    def handle(self) -> None:
        sock = self.connection
        greeting = sock.recv(2)
        methods = sock.recv(greeting[1]) if len(greeting) == 2 else b""
        with self.lock:
            type(self).seen.append(greeting + methods)
        sock.sendall(b"\x05\x02" if b"\x02" in methods else b"\x05\x00")
        if b"\x02" in methods:
            auth = sock.recv(2)
            rest = sock.recv(auth[1] + 1) if len(auth) == 2 else b""
            length = rest[-1] if rest else 0
            rest += sock.recv(length)
            with self.lock:
                type(self).seen.append(b"\x01" + auth[1:] + rest)
            sock.sendall(b"\x01\x00")
        head = sock.recv(4)                      # VER CMD RSV ATYP
        frame = head
        if head[3] == 0x03:                      # 域名形态：长度字节 + 域名
            length = sock.recv(1)
            frame += length + sock.recv(length[0])
        elif head[3] == 0x01:
            frame += sock.recv(4)
        port = sock.recv(2)
        frame += port
        with self.lock:
            type(self).seen.append(frame)
        sock.sendall(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + b"\x00\x00")
        time.sleep(0.05)

    def log_message(self, *args):
        return


@pytest.fixture
def socks_proxy():
    FakeSocks.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSocks)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[0], server.server_address[1]
    yield f"socks5h://pooluser:poolpass@{host}:{port}"
    server.shutdown()
    server.server_close()


def test_socks_handshake_sends_greeting_auth_and_a_domain_connect(socks_proxy):
    """greeting 要同时提供 user/pass 与 no-auth；CONNECT 必须用域名形态（=socks5h）。"""
    dialer = ts.Socks5Dialer(socks_proxy, timeout=5)
    sock = dialer.open("chat.qwen.ai", 443)
    sock.close()
    time.sleep(0.1)
    greeting, auth, connect = FakeSocks.seen[:3]
    # NMETHODS=1：有凭据就只提供 user/pass（不试匿名 —— 池子本来也不收匿名）
    assert greeting == b"\x05\x01\x02"
    assert auth[:2] == b"\x01\x08" and b"pooluser" in auth   # ULEN=8
    assert b"poolpass" in auth
    assert connect[:4] == b"\x05\x01\x00\x03"                # CONNECT + 域名形态
    assert connect[4] == len("chat.qwen.ai")
    assert b"chat.qwen.ai" in connect and connect.endswith(b"\x01\xbb")


def test_each_call_opens_a_new_connection(socks_proxy):
    """轮换靠的就是"每次新连接"：绝不能在这里做连接复用。"""
    dialer = ts.Socks5Dialer(socks_proxy, timeout=5)
    for _ in range(3):
        dialer.open("chat.qwen.ai").close()
    time.sleep(0.15)
    assert len([x for x in FakeSocks.seen if x.startswith(b"\x05\x01\x00\x03")]) == 3


# ------------------------------------------------------------- token 池语义

def test_a_second_take_is_served_from_the_cache():
    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL,
                        mint=lambda email, password: "token-" + email)
    first, cached_first = pool.take("a@x")
    second, cached_second = pool.take("a@x")
    assert (first, cached_first) == ("token-a@x", False)
    assert (second, cached_second) == ("token-a@x", True)
    assert pool.mints == 1 and pool.depth() == 1


def test_expired_tokens_are_minted_again():
    minted: list[str] = []

    def mint(email, password):
        minted.append(email)
        return f"token-{len(minted)}"

    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, ttl=0.05, mint=mint)
    assert pool.take("a@x")[0] == "token-1"
    time.sleep(0.08)
    assert pool.take("a@x")[0] == "token-2"          # TTL 过了 ⇒ 重铸（JWT 无状态，换一份无害）
    assert minted == ["a@x", "a@x"]


def test_unknown_account_is_refused_without_minting():
    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=lambda e, p: "t")
    with pytest.raises(ts.MintError):
        pool.take("other@x")
    assert pool.mints == 0


def test_a_failing_mint_is_reported_and_not_cached():
    def boom(email, password):
        raise ts.WallError("this egress is answering the WAF challenge page")

    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=boom)
    with pytest.raises(ts.WallError):
        pool.take("a@x")
    assert pool.depth() == 0 and "WAF" in (pool.last_error or "")
    with pytest.raises(ts.WallError):
        pool.take("a@x")                              # 再试仍是失败，而不是给出空 token


def test_concurrent_takes_for_one_account_mint_once():
    """同一账号并发只登一次：这里每多登一次就是多一次撞墙机会。"""
    calls: list[str] = []
    started = threading.Barrier(4)

    def slow_mint(email, password):
        calls.append(email)
        time.sleep(0.05)
        return "token"

    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=slow_mint)
    out: list[tuple[str, bool]] = []

    def take():
        started.wait()
        out.append(pool.take("a@x"))

    threads = [threading.Thread(target=take) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1 and pool.mints == 1
    assert [c for _t, c in out].count(False) == 1     # 只有一发是真登的


# --------------------------------------------------------------- HTTP 面

@pytest.fixture
def service():
    servers = []

    def serve(pool, token=""):
        server = ts.build_server(pool, host="127.0.0.1", port=0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_port

    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str, token: str = ""):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": "Bearer " + token} if token else {}
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = json.loads(resp.read().decode("utf-8"))
    found = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, body, found


def test_token_endpoint_serves_a_token(service):
    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=lambda e, p: "tok-a")
    port = service(pool)
    status, body, _ = _get(port, "/token?account=a%40x")
    assert status == 200 and body["token"] == "tok-a" and body["cached"] is False
    status, body, _ = _get(port, "/token?account=a%40x")
    assert body["cached"] is True


def test_a_mint_failure_is_503_with_a_retry_hint(service):
    def boom(email, password):
        raise ts.WallError("walled")

    port = service(ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=boom))
    status, body, headers = _get(port, "/token?account=a%40x")
    assert status == 503 and "walled" in body["error"]
    assert headers["retry-after"] == str(ts.RETRY_AFTER_SECONDS)


def test_health_reports_pool_state(service):
    pool = ts.TokenPool({"a@x": "pw", "b@x": "pw"}, PROXY_URL, mint=lambda e, p: "t")
    port = service(pool)
    status, body, _ = _get(port, "/health")
    assert status == 200 and body["accounts"] == 2 and body["tokens_cached"] == 0


def test_the_endpoint_can_require_a_bearer_token(service):
    pool = ts.TokenPool({"a@x": "pw"}, PROXY_URL, mint=lambda e, p: "t")
    port = service(pool, token="secret")
    assert _get(port, "/health")[0] == 401
    assert _get(port, "/health", token="secret")[0] == 200


def test_socks_url_is_parsed_with_the_password_intact():
    host, port, user, password = ts.parse_socks_url(PROXY_URL)
    assert (host, port) == ("127.0.0.1", 1)
    assert (user, password) == ("pooluser", "poolpass")
    with pytest.raises(ValueError):
        ts.parse_socks_url("socks5h://host-without-port")
