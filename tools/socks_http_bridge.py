"""A local HTTP proxy that dials a rotating SOCKS5 pool, one tunnel per request.

Why this exists (`adapter/proxyplan.py` is the other half):

* aiohttp speaks HTTP proxies, not SOCKS, and the pool is SOCKS5-only;
* the pool hands out a **new address per connection**, so "one address per
  client request" really means "do not reuse the previous request's socket".
  The adapter gets that by giving every request its own session;
* the adapter tags its calls with the request's session id (sent as the proxy
  login) so a call can be attributed to the request that made it. Today the id
  is **recorded and reported, not used for routing**, and that is a deliberate
  limitation rather than an unfinished one: for an https target the tunnel is a
  byte pipe around a *stateful* TLS session, so a tunnel cannot be handed to a
  different client connection without breaking the handshake already inside it.
  One client connection is one tunnel is one exit. If a connection dies
  mid-request and aiohttp dials again, that dial gets a different address --
  visible in `/health`, and unfixable here. Preventing it needs a pool with
  session-key stickiness, which this bridge is already shaped to carry.

What it deliberately does not do:

* **terminate TLS.** The tunnel carries opaque bytes, so the adapter's TLS
  still runs end to end to the vendor and this process never sees a body it
  could log by accident.
* **share a tunnel between concurrent calls.** One tunnel serves one call at a
  time, so two concurrent calls from the same session open two tunnels and get
  two addresses. That is what a per-connection pool means; pretending otherwise
  would be a promise this code cannot keep.

    python tools/socks_http_bridge.py \\
        --listen 127.0.0.1:11080 \\
        --upstream 'socks5h://user:pass@pool.example:2088' \\
        --bypass '*.aliyuncs.com'

`--bypass` hosts are dialled directly, without the pool: an object-store upload
is the case it exists for. `/health` reports tunnel counts and the last errors.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# `tools/` is not a package, and the SOCKS5 handshake in token_service.py is
# already proven against this same pool -- a second copy would be a second
# place for the greeting bytes to be wrong.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from token_service import Socks5Dialer, parse_socks_url  # noqa: E402

#: How long an idle tunnel may sit in the pool before it is closed. Long enough
#: to cover the gap between two calls of one request (`chats/new` -> generation
#: -> fallback read), short enough that a quiet channel does not hold exits
#: open indefinitely.
DEFAULT_IDLE_TTL = 120.0

#: Read size for streaming. Response bodies are forwarded chunk by chunk, so
#: this is a bandwidth knob, not a memory one.
CHUNK = 65536

MAX_HEADER_BYTES = 65536


@dataclass(frozen=True)
class TunnelKey:
    """One upstream tunnel is identified by *who* asked and *where* it goes."""

    session: str
    host: str
    port: int

    def __str__(self) -> str:  # for logs
        return f"{self.session or '<none>'}->{self.host}:{self.port}"


# --------------------------------------------------------------------- pool


class TunnelPool:
    """Idle upstream tunnels, keyed by (session, target) and reused in order.

    A tunnel taken out is never handed to a second caller -- `acquire` pops it.
    That is what makes the pool safe without per-tunnel locking, and it is also
    why a concurrent call from the same session simply opens another tunnel.
    """

    def __init__(
        self,
        upstream: str,
        *,
        dial_timeout: float = 30.0,
        idle_ttl: float = DEFAULT_IDLE_TTL,
    ) -> None:
        # The dialer parses the URL itself; the host is kept only so the startup
        # line can name the pool without echoing credentials.
        self.upstream_host = urlsplit(upstream).hostname or ""
        self._dialer = Socks5Dialer(upstream, timeout=dial_timeout)
        self.idle_ttl = idle_ttl
        self._idle: dict[TunnelKey, list[tuple[socket.socket, float]]] = {}
        self._lock = threading.Lock()
        self.opened = 0
        self.reused = 0
        self.errors: list[str] = []
        self.sessions: list[str] = []

    def _dial(self, key: TunnelKey) -> socket.socket:
        sock = self._dialer.open(key.host, key.port)
        self.opened += 1
        return sock

    def acquire(self, key: TunnelKey) -> tuple[socket.socket, bool]:
        """(tunnel, reused). A fresh dial when nothing idle is available."""
        with self._lock:
            bucket = self._idle.get(key) or []
            while bucket:
                sock, _ts = bucket.pop()
                if not bucket:
                    self._idle.pop(key, None)
                self.reused += 1
                return sock, True
        return self._dial(key), False

    def release(self, key: TunnelKey, sock: socket.socket) -> None:
        """Put a healthy tunnel back for the next call in this session."""
        with self._lock:
            self._idle.setdefault(key, []).append((sock, time.time()))

    def discard(self, sock: socket.socket) -> None:
        try:
            sock.close()
        except OSError:
            pass

    def note_error(self, detail: str) -> None:
        self.errors.append(detail[:200])
        del self.errors[:-5]

    def note_session(self, session: str) -> None:
        """Remember which request a call belonged to, for /health.

        Not used for routing -- the tunnel key already carries the id -- but
        "which request opened this tunnel" is the first question when an exit
        looks wrong, and the alternative is guessing from timestamps.
        """
        with self._lock:
            self.sessions.append(session)
            del self.sessions[:-8]

    def sweep(self) -> int:
        """Close tunnels idle past the TTL. Returns how many were closed."""
        now = time.time()
        closed = 0
        with self._lock:
            for key in list(self._idle):
                keep = []
                for sock, ts in self._idle[key]:
                    if now - ts > self.idle_ttl:
                        self.discard(sock)
                        closed += 1
                    else:
                        keep.append((sock, ts))
                if keep:
                    self._idle[key] = keep
                else:
                    self._idle.pop(key, None)
        return closed

    def close_all(self) -> None:
        with self._lock:
            for bucket in self._idle.values():
                for sock, _ts in bucket:
                    self.discard(sock)
            self._idle.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            idle = sum(len(b) for b in self._idle.values())
            sessions = len({k.session for k in self._idle})
        return {
            "tunnels_opened": self.opened,
            "tunnels_reused": self.reused,
            "tunnels_idle": idle,
            "sessions_idle": sessions,
            "recent_sessions": list(self.sessions),
            "last_errors": self.errors[-3:],
        }


# -------------------------------------------------------------- http helpers


class HttpError(Exception):
    """Something about the request cannot be forwarded as written."""


def parse_request_line(line: bytes) -> tuple[str, str, str]:
    try:
        method, target, version = line.decode("latin-1").strip().split(" ", 2)
    except ValueError as exc:
        raise HttpError(f"malformed request line: {line[:80]!r}") from exc
    return method.upper(), target, version


def read_headers(rfile) -> tuple[list[tuple[str, str]], int]:
    """Returns (headers, bytes_read). Raises when the block is absurdly large."""
    headers: list[tuple[str, str]] = []
    total = 0
    while True:
        line = rfile.readline()
        if not line:
            raise HttpError("client closed before sending headers")
        total += len(line)
        if total > MAX_HEADER_BYTES:
            raise HttpError("request headers too large")
        if line in (b"\r\n", b"\n"):
            return headers, total
        name, _, value = line.decode("latin-1").partition(":")
        headers.append((name.strip().lower(), value.strip()))


def header_value(headers: list[tuple[str, str]], name: str) -> str:
    for key, value in headers:
        if key == name:
            return value
    return ""


def target_host_port(target: str, headers: list[tuple[str, str]], *, default_port: int) -> tuple[str, int, str]:
    """(host, port, path) for an absolute-URI request target."""
    parts = urlsplit(target)
    host = parts.hostname or header_value(headers, "host").split(":")[0]
    if not host:
        raise HttpError(f"cannot tell where {target!r} goes")
    port = parts.port or (443 if parts.scheme == "https" else default_port)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def session_of(headers: list[tuple[str, str]]) -> str:
    """The request's session id, sent as the proxy login.

    `Proxy-Authorization: Basic base64(<session>:<anything>)` -- aiohttp builds
    that from `proxy_auth=BasicAuth(session_id, "x")`. Absent (a caller that
    does not rotate) it yields "", and the empty string is itself a valid key:
    those calls share one tunnel, which is what "not rotating" means.
    """
    raw = header_value(headers, "proxy-authorization")
    if not raw.lower().startswith("basic "):
        return ""
    try:
        decoded = base64.b64decode(raw[6:].strip(), validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""
    return decoded.split(":", 1)[0].strip()


def _body_plan(headers: list[tuple[str, str]]) -> tuple[str, int]:
    """('length'|'chunked'|'none', content_length)."""
    encoding = header_value(headers, "transfer-encoding").lower()
    if "chunked" in encoding:
        return "chunked", 0
    raw = header_value(headers, "content-length")
    if raw:
        try:
            return "length", int(raw)
        except ValueError as exc:
            raise HttpError(f"bad content-length: {raw!r}") from exc
    return "none", 0


def read_body(rfile, plan: tuple[str, int]) -> bytes:
    kind, length = plan
    if kind == "length":
        data = b""
        while len(data) < length:
            chunk = rfile.read(length - len(data))
            if not chunk:
                raise HttpError("client closed mid-body")
            data += chunk
        return data
    if kind == "chunked":
        # Forwarded verbatim, so a decoder here would only add a place to be
        # wrong. The terminator is what has to be found.
        data = b""
        while True:
            line = rfile.readline()
            if not line:
                raise HttpError("client closed mid-chunk")
            data += line
            size = int(line.strip().split(b";")[0] or b"0", 16)
            payload = rfile.read(size + 2)
            if len(payload) < size + 2:
                raise HttpError("client closed mid-chunk")
            data += payload
            if size == 0:
                while True:  # trailers
                    trailer = rfile.readline()
                    data += trailer
                    if trailer in (b"\r\n", b"\n", b""):
                        return data
    return b""


def relay_response(tunnel_file, wfile) -> bool:
    """Copy one response to the client. Returns whether the tunnel stays usable.

    Streamed, not buffered: the generation call is an SSE stream that runs for
    up to a minute, and a proxy that waited for the body would turn a stream
    into a timeout.
    """
    status_line = tunnel_file.readline()
    if not status_line:
        raise HttpError("upstream closed before the status line")
    wfile.write(status_line)
    headers: list[tuple[str, str]] = []
    while True:
        line = tunnel_file.readline()
        if not line:
            raise HttpError("upstream closed mid-headers")
        wfile.write(line)
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers.append((name.strip().lower(), value.strip()))

    close_after = "close" in header_value(headers, "connection").lower()
    encoding = header_value(headers, "transfer-encoding").lower()
    length = header_value(headers, "content-length")

    if "chunked" in encoding:
        while True:
            line = tunnel_file.readline()
            if not line:
                return False
            wfile.write(line)
            size = int(line.strip().split(b";")[0] or b"0", 16)
            if size == 0:
                while True:  # trailers
                    trailer = tunnel_file.readline()
                    wfile.write(trailer)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            payload = tunnel_file.read(size + 2)
            wfile.write(payload)
    elif length:
        remaining = int(length)
        while remaining > 0:
            chunk = tunnel_file.read(min(CHUNK, remaining))
            if not chunk:
                return False
            wfile.write(chunk)
            remaining -= len(chunk)
    else:
        # No length and no chunking: the only terminator is EOF, so the tunnel
        # is spent either way.
        while True:
            chunk = tunnel_file.read(CHUNK)
            if not chunk:
                break
            wfile.write(chunk)
        wfile.flush()
        return False

    wfile.flush()
    return not close_after


# ------------------------------------------------------------------- handler


class BridgeHandler(socketserver.BaseRequestHandler):
    pool: TunnelPool
    bypass: tuple[str, ...]
    log: Any

    def handle(self) -> None:  # noqa: C901 - one linear request loop reads better
        client = self.request
        rfile = client.makefile("rb")
        wfile = client.makefile("wb")
        try:
            while True:
                line = rfile.readline()
                if not line:
                    return
                method, target, _version = parse_request_line(line)
                headers, _ = read_headers(rfile)

                if method == "CONNECT":
                    self._connect(target, headers, client)
                    return

                # The bridge's own health endpoint, asked for as origin-form.
                if target == "/health" and method == "GET":
                    self._health(wfile)
                    return

                if not self._keep_alive(rfile, wfile, method, target, headers):
                    return
        except HttpError as exc:
            self.log(f"http error: {exc}")
            self._fail(wfile, 400, str(exc))
        except (OSError, ValueError) as exc:
            self.log(f"transport error: {type(exc).__name__}: {exc}")
        finally:
            try:
                wfile.close()
                rfile.close()
            except OSError:
                pass

    # -- paths --------------------------------------------------------------

    def _connect(self, target: str, headers, client) -> None:
        """Tunnel bytes for a TLS connection: opaque, end to end."""
        host, _, rest = target.partition(":")
        port = int(rest or "443")
        session = session_of(headers)
        self.pool.note_session(session)
        key = TunnelKey(session, host, port)
        try:
            tunnel, reused = self._open(key, headers)
        except Exception as exc:  # noqa: BLE001 - any dial failure owes a status line
            # `Exception`, not `OSError`. A SOCKS greeting answered with `0xff`
            # raises `MintError`, and letting that escape closes the client
            # connection *without a status line*: measured 2026-09-20 with a
            # stub pool that declined the offered auth methods, where curl said
            # `Proxy CONNECT aborted` instead of a 502. A proxy that cannot
            # reach its upstream owes the client a status; the reason belongs in
            # the log and `/health`, not in a truncated connection.
            self._note(f"CONNECT {key} failed: {type(exc).__name__}: {exc}")
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        if not reused:
            self.log(f"CONNECT {key} via pool")
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        # Never returned to the pool: this tunnel wraps a TLS session belonging
        # to *this* client connection, so handing it to another one would break
        # the handshake inside it. One connection, one exit -- and a re-dial
        # after a drop is a new exit (see the module docstring).
        try:
            self._pipe(client, tunnel)
        finally:
            self.pool.discard(tunnel)

    def _keep_alive(self, rfile, wfile, method: str, target: str, headers) -> bool:
        """Forward one absolute-URI request. False means the client is done."""
        if target.startswith("/"):
            # Origin-form on a proxy port is either a mistake or the health
            # endpoint, which `handle` already answered.
            self._fail(wfile, 400, "expected an absolute-URI request target")
            return False
        host, port, path = target_host_port(target, headers, default_port=80)

        session = session_of(headers)
        self.pool.note_session(session)
        key = TunnelKey(session, host, port)
        body = read_body(rfile, _body_plan(headers))

        # Proxy-only headers are dropped, and the request line becomes
        # origin-form: the vendor is not a proxy and must not be told it is one.
        forwarded = b"".join(
            f"{name}: {value}\r\n".encode("latin-1")
            for name, value in headers
            if name not in {"proxy-authorization", "proxy-connection"}
        )

        attempt = 0
        while attempt < 2:
            attempt += 1
            try:
                tunnel, reused = self._open(key, headers)
            except Exception as exc:  # noqa: BLE001 - same rule as the CONNECT path
                self._note(f"{key} dial failed: {type(exc).__name__}: {exc}")
                self._fail(wfile, 502, f"cannot reach {host}:{port}")
                return False
            try:
                tunnel.sendall(
                    f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
                    + forwarded
                    + b"\r\n"
                    + body
                )
                with tunnel.makefile("rb") as tunnel_file:
                    reusable = relay_response(tunnel_file, wfile)
            except (OSError, HttpError) as exc:
                # A pooled tunnel that died while idle looks like this. One
                # retry on a fresh dial, then give up: retrying a POST is not
                # free, and this proxy does not decide that on the caller's
                # behalf beyond recovering its own dead socket.
                self._note(f"{key} forward failed: {exc}")
                self.pool.discard(tunnel)
                if attempt == 1 and not reused:
                    self._fail(wfile, 502, "upstream connection failed")
                    return False
                continue
            if reusable:
                self.pool.release(key, tunnel)
            else:
                self.pool.discard(tunnel)
            return header_value(headers, "connection").lower() != "close"
        self._fail(wfile, 502, "upstream connection failed")
        return False

    def _open(self, key: TunnelKey, headers) -> tuple[socket.socket, bool]:
        if self._is_bypassed(key.host):
            # Direct, no pool: bypassed hosts are not what the pool is for.
            sock = socket.create_connection((key.host, key.port), 30.0)
            return sock, False
        return self.pool.acquire(key)

    def _is_bypassed(self, host: str) -> bool:
        host = host.lower()
        for pattern in self.bypass:
            if pattern == "*":
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
            if host == pattern:
                return True
        return False

    def _health(self, wfile) -> None:
        payload = json.dumps(self.pool.stats(), ensure_ascii=False).encode()
        wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\n\r\n"
            + payload
        )
        wfile.flush()

    @staticmethod
    def _fail(wfile, status: int, detail: str) -> None:
        body = json.dumps({"error": detail}, ensure_ascii=False).encode()
        try:
            wfile.write(
                f"HTTP/1.1 {status} Error\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            wfile.flush()
        except OSError:
            pass

    def _note(self, detail: str) -> None:
        self.log(detail)
        self.pool.note_error(detail)

    @staticmethod
    def _pipe(client: socket.socket, tunnel: socket.socket) -> None:
        """Byte pump until either side closes."""
        sockets = [client, tunnel]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 30.0)
                if not readable:
                    continue
                for sock in readable:
                    data = sock.recv(CHUNK)
                    if not data:
                        return
                    (tunnel if sock is client else client).sendall(data)
        except OSError:
            return


# ---------------------------------------------------------------------- main


class BridgeServer(socketserver.ThreadingTCPServer):
    """Threaded TCP server, plus the pool and the health counters."""

    daemon_threads = True
    # Set on the class, not the instance: TCPServer binds in __init__, so an
    # instance attribute assigned afterwards would be too late to matter.
    allow_reuse_address = True

    def __init__(self, address, handler, pool: TunnelPool) -> None:
        super().__init__(address, handler)
        self.pool = pool


def build_server(
    listen: str, upstream: str, *, bypass: tuple[str, ...], idle_ttl: float,
    quiet: bool = False,
) -> BridgeServer:
    host, _, port = listen.rpartition(":")
    pool = TunnelPool(upstream, idle_ttl=idle_ttl)

    def log(detail: str) -> None:
        if not quiet:
            print(f"[bridge] {detail}", file=sys.stderr, flush=True)

    handler = type(
        "BoundBridgeHandler",
        (BridgeHandler,),
        # staticmethod, not a bare function: a plain function assigned to a
        # class attribute becomes a bound method, and `self.log(detail)` would
        # then be called with the instance as an extra argument.
        {"pool": pool, "bypass": bypass, "log": staticmethod(log)},
    )
    server = BridgeServer((host or "127.0.0.1", int(port)), handler, pool)

    def sweeper() -> None:
        while True:
            time.sleep(min(30.0, idle_ttl / 2 or 30.0))
            pool.sweep()

    threading.Thread(target=sweeper, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen", default="127.0.0.1:11080")
    parser.add_argument(
        "--upstream",
        default=os.environ.get("SOCKS_BRIDGE_UPSTREAM", ""),
        help="rotating SOCKS5 pool, e.g. socks5h://user:pass@host:2088. "
             "Defaults to $SOCKS_BRIDGE_UPSTREAM, and that is the form to use "
             "in a deployment: the pool credential carries the exit, and a "
             "command line is readable by every process on the host (`ps`), "
             "while an env file is not.",
    )
    parser.add_argument(
        "--bypass",
        action="append",
        default=[],
        help="host pattern dialled directly (repeatable, *.suffix allowed)",
    )
    parser.add_argument("--idle-ttl", type=float, default=DEFAULT_IDLE_TTL)
    args = parser.parse_args(argv)
    if not args.upstream:
        # Said out loud rather than defaulting to something: a bridge without an
        # upstream would either go direct (silently not rotating) or fail on the
        # first request, and both look like "the proxy is configured".
        parser.error("--upstream is required, or set SOCKS_BRIDGE_UPSTREAM")

    bypass = tuple(p.strip().lower() for p in args.bypass if p.strip())
    server = build_server(
        args.listen, args.upstream, bypass=bypass, idle_ttl=args.idle_ttl
    )
    pool: TunnelPool = server.pool
    print(
        f"[bridge] listening on {args.listen} -> {pool.upstream_host}"
        f" (bypass={list(bypass) or 'none'}, idle_ttl={args.idle_ttl:.0f}s)",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.close_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
