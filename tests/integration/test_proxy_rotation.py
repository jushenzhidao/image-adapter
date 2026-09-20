"""One client request, one exit -- asserted with a real bridge and real sockets.

The unit tests pin the two halves separately: `test_upstream_proxy.py` that the
adapter builds the right aiohttp arguments, `test_socks_http_bridge.py` that the
bridge groups calls by the session id it is handed. Neither can see the seam
between them, and the seam is where the assumptions are:

* aiohttp has to actually put `Proxy-Authorization` on the proxy hop when given
  `proxy_auth=BasicAuth(session_id, "x")` -- if it did not, every call would land
  in one anonymous session;
* the bridge has to read that back out and group by it.

Get either wrong and rotation looks configured while doing nothing at all: the
channel would keep one exit for the life of the worker, and nothing in a trace
would say so. So these tests run the two together.

Connection count is the assertion, because a connection *is* an exit on a pool
that hands out an address per connection. The fake pool below is the pool and
the target in one socket: after the SOCKS5 handshake it just answers HTTP, which
is all any client above it can tell about where the bytes came from.
"""

from __future__ import annotations

import importlib.util
import socket
import ssl
import struct
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from adapter.channel import parse_channel
from adapter.context import AdapterContext
from adapter.ctxapi import RequestPlan
from adapter.ctxapi.proxy_http import ProxiedHttp
from adapter.settings import Settings
from adapter.transport import build_request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _self_signed(directory: Path) -> ssl.SSLContext:
    """A throwaway server certificate for the fake pool.

    Generated at test time rather than shipped: a private key in the repository
    is a liability even when it is provably worthless. And a missing openssl is
    a *failure*, not a skip -- a skipped test here would be a green light that
    proves nothing about the one mechanism this file exists to check.
    """
    key = directory / "key.pem"
    crt = directory / "crt.pem"
    result = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(crt), "-days", "1",
            "-subj", "/CN=api.vendor.test",
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(crt), str(key))
    return context



def _load_bridge():
    spec = importlib.util.spec_from_file_location(
        "socks_http_bridge_integration", REPO_ROOT / "tools" / "socks_http_bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["socks_http_bridge_integration"] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


class FakeSocksPool:
    """A SOCKS5 server that then speaks HTTP: pool and target in one socket."""

    def __init__(self, tls: ssl.SSLContext | None = None) -> None:
        self.tls = tls
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        #: One entry per dial. This is the exit count.
        self.connections: list[socket.socket] = []
        self._stop = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

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
            greeting = conn.recv(2)
            conn.recv(greeting[1])
            conn.sendall(b"\x05\x00")  # no authentication
            head = conn.recv(4)
            if head[3] == 0x03:
                length = conn.recv(1)[0]
                conn.recv(length)
            elif head[3] == 0x01:
                conn.recv(4)
            else:
                raise ValueError("unsupported address type")
            struct.unpack("!H", conn.recv(2))[0]
            conn.sendall(
                b"\x05\x00\x00\x01"
                + socket.inet_aton("127.0.0.1")
                + struct.pack("!H", 443)
            )
            if self.tls is not None:
                # The tunnel ends at the pool, so the TLS handshake the adapter
                # started lands here -- exactly where it lands against the real
                # vendor. The bridge carries the bytes without ever seeing them.
                conn = self.tls.wrap_socket(conn, server_side=True)
                conn.settimeout(10.0)
            self._serve_http(conn)
        except (OSError, ValueError, ssl.SSLError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_http(self, conn: socket.socket) -> None:
        rfile = conn.makefile("rb")
        while True:
            line = rfile.readline()
            if not line:
                return
            length = 0
            while True:
                header = rfile.readline()
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
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
def pool(tmp_path):
    fake = FakeSocksPool(tls=_self_signed(tmp_path))
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture()
def bridge_port(pool):
    server = bridge.build_server(
        "127.0.0.1:0",
        f"socks5h://user:pw@127.0.0.1:{pool.port}",
        bypass=(),
        idle_ttl=30.0,
        quiet=True,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        server.pool.close_all()


def _settings() -> Settings:
    # `_env_file=None`: a bare Settings() would read the production .env.
    return Settings(
        _env_file=None,
        adapter_key_required=False,
        upstream_proxy_allowlist="127.0.0.1",
    )


def _channel(port: int, session_id: str):
    """A channel that rotates, with the id the pipeline would have attached.

    https on purpose, and that is not decoration: aiohttp emits proxy headers
    only on the CONNECT it opens for an https target. Point this at plain http
    and the session id is dropped without a word -- measured while writing this
    file, where the plain-http version passed for the wrong reason (an
    anonymous session reuses its tunnel too, so "one request, one exit" held
    anyway and proved nothing).
    """
    headers = {
        "x-upstream-url": "https://api.vendor.test/v1/images/generations",
        "x-script-ref": "qwen/images@v1",
        "x-upstream-proxy": f"http://127.0.0.1:{port}",
        "x-upstream-proxy-mode": "per-request",
    }
    channel = parse_channel(headers, _settings())
    return replace(channel, proxy=channel.proxy.with_session(session_id))


async def _call(session: aiohttp.ClientSession, channel) -> None:
    url, method, kwargs = build_request(channel, RequestPlan(), {"prompt": "x"}, None)
    # `ssl=False` only disables verification of the throwaway certificate the
    # fake pool serves; the handshake itself is real.
    async with session.request(method, url, ssl=False, **kwargs) as response:
        assert response.status == 200
        assert await response.json() == {"ok": True}


async def test_one_request_keeps_all_its_calls_on_one_exit(pool, bridge_port) -> None:
    """A request's own calls share one tunnel.

    What this measures is aiohttp's side of it: with one proxy credential and
    one fingerprint, the connection pool hands back the same CONNECT tunnel for
    every call in the request. (The session id is not what does this -- see
    `test_the_bridge_records_which_request_each_call_belonged_to` for why the
    exit count alone cannot see the id at all.)
    """
    channel = _channel(bridge_port, "request-a")

    async with aiohttp.ClientSession() as session:
        for _ in range(3):
            await _call(session, channel)

    assert len(pool.connections) == 1, "calls from one request landed on separate exits"


async def test_a_second_request_does_not_inherit_the_first_exit(
    pool, bridge_port
) -> None:
    """Rotation: the next client request must dial its own tunnel.

    One session per client request is not a detail of this test, it *is* the
    mechanism (`AdapterContext._script_session`, built in `adapt`): the pool
    hands a fresh address per connection, so a new exit means a new connection,
    which means a pool that was discarded with the previous request. Nothing
    here relies on the proxy credential taking part in aiohttp's connection
    key -- measured 2026-09-19: it does not (it did while the deprecated
    `proxy_auth` parameter was used, which is what this test caught).
    """
    for session_id in ("request-a", "request-b"):
        async with aiohttp.ClientSession() as session:
            await _call(session, _channel(bridge_port, session_id))

    assert len(pool.connections) == 2, "the second request reused the first exit"


async def test_the_bridge_records_which_request_each_call_belonged_to(
    pool, bridge_port
) -> None:
    """The seam that counting exits cannot see.

    With one session per client request, two requests dial twice no matter what
    the session id says, so `len(pool.connections)` proves the rotation and
    proves nothing about the id surviving the hop. This assertion does: the
    bridge reports the ids it read off the wire, and a mutation that drops
    `proxy_headers` (measured 2026-09-19) turns them into `["", ""]` while
    every other assertion in this file still passes.
    """
    for session_id in ("request-a", "request-b"):
        async with aiohttp.ClientSession() as session:
            await _call(session, _channel(bridge_port, session_id))

    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{bridge_port}/health") as response:
            stats = await response.json()

    assert stats["recent_sessions"] == ["request-a", "request-b"]


async def test_the_context_owns_and_closes_the_request_session(bridge_port) -> None:
    """`ctx.close()` is what stops the next request inheriting this pool."""
    channel = _channel(bridge_port, "request-a")
    request_http = aiohttp.ClientSession()
    ctx = AdapterContext(
        request_id="req-1",
        channel=channel,
        settings=_settings(),
        http=None,
        request_http=request_http,
    )

    view = ctx.http
    assert isinstance(view, ProxiedHttp), "a proxied channel must not hand out a raw session"
    # The download path is the same socket and a different view: material
    # downloads never carry the channel's exit.
    assert ctx.download_http is request_http

    await ctx.close()
    assert request_http.closed, "the request session outlived the request"
