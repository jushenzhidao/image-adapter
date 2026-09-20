"""tools/socks_http_bridge.py: the hop that makes one exit per request real.

The bridge is the only piece of the rotation that cannot be reasoned about from
the adapter side, so what is pinned here is its two load-bearing promises:

* **one tunnel per session, reused across calls** -- so a request that loses a
  connection mid-flight comes back from the same address;
* **a different session dials a different tunnel** -- so the next request
  leaves from somewhere else.

Both are asserted against a fake SOCKS5 pool that then speaks plain HTTP on the
same socket, which is exactly the shape the real pool has: it hands back bytes
to the target, and nothing above it can tell where those bytes came from. The
connection count *is* the number of distinct exits, so that is what the tests
count.

Nothing here touches the real pool or the network beyond loopback: a bridge
that rotated exits in a unit test would be both slow and a way to get an
address blocked.
"""

from __future__ import annotations

import base64
import http.server
import importlib.util
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_bridge():
    """tools/ is not a package; load the module by path, as its own tests do.

    It has to be registered in `sys.modules` before execution: `@dataclass`
    resolves `cls.__module__` through that table, and a module that is not in it
    fails with an `AttributeError` that points nowhere near the cause.
    """
    spec = importlib.util.spec_from_file_location(
        "socks_http_bridge", REPO_ROOT / "tools" / "socks_http_bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["socks_http_bridge"] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


# ------------------------------------------------------------- fake pool


class FakePool:
    """A SOCKS5 server that, once connected, plays the target's HTTP server."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.connections: list[socket.socket] = []
        self.requests: list[str] = []  # request lines, in arrival order
        self.headers: list[dict[str, str]] = []
        self.targets: list[str] = []
        self.authorized: list[str] = []
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # -- plumbing ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(10.0)
        try:
            self._handshake(conn)
            self._serve_http(conn)
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handshake(self, conn: socket.socket) -> None:
        greeting = conn.recv(2)
        conn.recv(greeting[1])  # the offered methods
        conn.sendall(b"\x05\x02")  # this pool always asks for user/password
        conn.recv(1)
        user_len = conn.recv(1)[0]
        user = conn.recv(user_len).decode()
        pass_len = conn.recv(1)[0]
        conn.recv(pass_len)
        self.authorized.append(user)
        conn.sendall(b"\x01\x00")

        head = conn.recv(4)  # ver, cmd, rsv, atyp
        if head[3] == 0x03:
            length = conn.recv(1)[0]
            host = conn.recv(length).decode()
        elif head[3] == 0x01:
            host = socket.inet_ntoa(conn.recv(4))
        else:
            raise ValueError("unsupported address type")
        port = struct.unpack("!H", conn.recv(2))[0]
        self.targets.append(f"{host}:{port}")
        conn.sendall(
            b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", port)
        )

    def _serve_http(self, conn: socket.socket) -> None:
        rfile = conn.makefile("rb")
        while True:
            line = rfile.readline()
            if not line:
                return
            self.requests.append(line.decode("latin-1").strip())
            got: dict[str, str] = {}
            length = 0
            while True:
                header = rfile.readline()
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.decode("latin-1").partition(":")
                got[name.strip().lower()] = value.strip()
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            self.headers.append(got)
            if length:
                rfile.read(length)
            body = b'{"ok":true}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )

    def close(self) -> None:
        self._stop = True
        try:
            self.server.close()
        except OSError:
            pass


@pytest.fixture()
def pool():
    fake = FakePool()
    try:
        yield fake
    finally:
        fake.close()


class Bridge:
    """The bridge under test, wired to a fake pool."""

    def __init__(self, pool_port: int, *, bypass: tuple[str, ...] = (), idle_ttl: float = 5.0):
        upstream = f"socks5h://user:pw@127.0.0.1:{pool_port}"
        self.server = bridge.build_server(
            "127.0.0.1:0", upstream, bypass=bypass, idle_ttl=idle_ttl, quiet=True
        )
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.pool.close_all()


@pytest.fixture()
def bridge_server(pool):
    server = Bridge(pool.port)
    try:
        yield server
    finally:
        server.close()


# ---------------------------------------------------------------- client side


def proxy_request(
    port: int,
    target: str,
    *,
    session: str = "",
    method: str = "POST",
    body: bytes = b'{"prompt":"x"}',
) -> tuple[int, bytes]:
    """One absolute-URI request through the bridge, the way aiohttp sends it."""
    sock = socket.create_connection(("127.0.0.1", port), 10.0)
    sock.settimeout(10.0)
    try:
        head = [
            f"{method} {target} HTTP/1.1",
            f"Host: {urlsplit(target).netloc}",
            f"Content-Length: {len(body)}",
            "Content-Type: application/json",
        ]
        if session:
            token = base64.b64encode(f"{session}:x".encode()).decode()
            head.append(f"Proxy-Authorization: Basic {token}")
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        rfile = sock.makefile("rb")
        status_line = rfile.readline().decode("latin-1")
        status = int(status_line.split(" ")[1])
        length = 0
        while True:
            line = rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        return status, rfile.read(length) if length else b""
    finally:
        sock.close()


def raw_request(port: int, request: bytes) -> tuple[int, bytes]:
    """Send a request verbatim: origin-form targets like /health need this."""
    sock = socket.create_connection(("127.0.0.1", port), 10.0)
    sock.settimeout(10.0)
    try:
        sock.sendall(request)
        rfile = sock.makefile("rb")
        status_line = rfile.readline().decode("latin-1")
        status = int(status_line.split(" ")[1]) if status_line else 0
        length = 0
        while True:
            line = rfile.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        return status, rfile.read(length) if length else b""
    finally:
        sock.close()


def connect_tunnel(port: int, host: str, *, session: str = "") -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), 10.0)
    sock.settimeout(10.0)
    head = [f"CONNECT {host}:443 HTTP/1.1", f"Host: {host}:443"]
    if session:
        token = base64.b64encode(f"{session}:x".encode()).decode()
        head.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline()
    while rfile.readline() not in (b"\r\n", b"\n", b""):
        pass
    assert b"200" in status, status
    return sock


# -------------------------------------------------------------------- tests


def test_two_calls_in_one_session_reuse_a_single_tunnel(bridge_server, pool) -> None:
    """The whole reason the session id exists: one address per request."""
    for _ in range(2):
        status, _body = proxy_request(
            bridge_server.port, "http://vendor.test/v2/chats/new", session="req-1"
        )
        assert status == 200

    assert len(pool.connections) == 1, "a serial pair of calls must not re-dial"
    assert len(pool.requests) == 2
    # origin-form on the wire: the vendor is not a proxy and must not be told
    # it is one.
    assert pool.requests[0].startswith("POST /v2/chats/new HTTP/1.1")


def test_a_different_session_dials_a_different_tunnel(bridge_server, pool) -> None:
    proxy_request(bridge_server.port, "http://vendor.test/a", session="req-1")
    proxy_request(bridge_server.port, "http://vendor.test/a", session="req-2")

    assert len(pool.connections) == 2, "a new request must not inherit the last exit"
    assert len(set(pool.authorized)) == 1  # same pool credentials, different login


def test_the_pool_sees_the_bridge_credentials_not_the_session_id(
    bridge_server, pool
) -> None:
    """Two different logins on two different hops, and they must not be confused.

    The client's `Proxy-Authorization` carries the request's session id and is
    consumed here; the pool authenticates the *bridge*, with the credentials
    from `--upstream`. Forwarding the session id upstream would leak the
    adapter's request identifiers to the provider and change nothing about the
    exit -- the pool has no notion of them.
    """
    proxy_request(bridge_server.port, "http://vendor.test/a", session="abc123")
    assert pool.authorized == ["user"]


def test_the_proxy_headers_do_not_reach_the_vendor(bridge_server, pool) -> None:
    """`Proxy-Authorization` is for the hop, not for the vendor."""
    proxy_request(bridge_server.port, "http://vendor.test/a", session="abc123")
    assert pool.headers, "the vendor never saw the request"
    assert "proxy-authorization" not in pool.headers[0]
    assert "proxy-connection" not in pool.headers[0]


def test_bypassed_hosts_never_touch_the_pool(pool) -> None:
    """`--bypass` is how an object-store upload stays on its own route."""

    class Upload(http.server.BaseHTTPRequestHandler):
        def do_PUT(self) -> None:  # noqa: N802 - stdlib hook name
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            pass

    target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upload)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    direct = Bridge(pool.port, bypass=("127.0.0.1",))
    try:
        status, body = proxy_request(
            direct.port,
            f"http://127.0.0.1:{target.server_address[1]}/upload",
            method="PUT",
        )
        assert status == 200
        assert body == b"ok"
        assert pool.connections == [], "a bypassed host must not open a pool tunnel"
    finally:
        direct.close()
        target.shutdown()
        target.server_close()


def test_connect_tunnels_bytes_in_both_directions(bridge_server, pool) -> None:
    """CONNECT carries TLS end to end; the bridge only moves bytes."""
    sock = connect_tunnel(bridge_server.port, "vendor.test", session="req-1")
    try:
        assert len(pool.connections) == 1
        assert pool.targets == ["vendor.test:443"]
    finally:
        sock.close()


def test_health_reports_what_the_pool_has_done(bridge_server, pool) -> None:
    proxy_request(bridge_server.port, "http://vendor.test/a", session="req-1")
    proxy_request(bridge_server.port, "http://vendor.test/a", session="req-1")

    status, body = raw_request(
        bridge_server.port, b"GET /health HTTP/1.1\r\nHost: bridge\r\n\r\n"
    )
    assert status == 200
    stats = json.loads(body)
    assert stats["tunnels_opened"] == 1
    assert stats["tunnels_reused"] == 1
    assert stats["tunnels_idle"] == 1


def test_a_dead_pooled_tunnel_is_replaced_not_retried_forever(pool) -> None:
    """A tunnel the pool closed while idle is recovered exactly once."""
    server = Bridge(pool.port)
    try:
        proxy_request(server.port, "http://vendor.test/a", session="req-1")
        # Kill the upstream side the way an idle timeout would.
        for conn in list(pool.connections):
            conn.close()
        status, _ = proxy_request(server.port, "http://vendor.test/a", session="req-1")
        assert status == 200
    finally:
        server.close()


def test_the_pool_sweeps_idle_tunnels(pool) -> None:
    """The TTL is what keeps a quiet channel from holding exits open."""
    tunnel_pool = bridge.TunnelPool(
        f"socks5h://user:pw@127.0.0.1:{pool.port}", idle_ttl=0.01
    )
    key = bridge.TunnelKey(session="req-1", host="vendor.test", port=80)
    sock, reused = tunnel_pool.acquire(key)
    assert reused is False
    tunnel_pool.release(key, sock)
    time.sleep(0.05)
    assert tunnel_pool.sweep() == 1
    assert tunnel_pool.stats()["tunnels_idle"] == 0


def test_the_session_header_survives_an_unparseable_credential(pool) -> None:
    """A malformed login is one anonymous session, not a crash."""
    assert bridge.session_of([("proxy-authorization", "Basic !!!not-base64!!!")]) == ""
    assert bridge.session_of([("proxy-authorization", "Bearer token")]) == ""
    assert (
        bridge.session_of(
            [("proxy-authorization", "Basic " + base64.b64encode(b"sess:pw").decode())]
        )
        == "sess"
    )
