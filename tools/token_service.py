"""Hand out qwen account tokens, signing in through the rotating proxy pool.

Why this lives in a tool instead of in the adapter script (see `docs/10` §3.2):

* the `signin` endpoint has an **IP-scoped** rate limit -- trip it and every
  later attempt from that egress gets an Aliyun challenge page for minutes
  (measured 2026-09-18: 8 accounts signing in within seconds walled an egress);
* the pool is **SOCKS5-only** (`http://` on either port fails), and aiohttp has
  no native SOCKS support;
* a script cannot open its own sockets (the sandbox allows no network stack), so
  the one place that can rotate the egress is out here.

The token is a stateless JWT, so signing in from a rotating IP and *using* it
from the adapter's normal egress is fine -- that split is the whole design.

Standard library only, no third-party SOCKS client: the handshake is small
enough to implement (greeting -> user/pass auth -> CONNECT), and doing it here
means the adapter image needs no new dependency.

    GET /token?account=<email>   {"token", "account", "cached", "minted_at"}
    GET /health                  pool state, cached accounts, last errors
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BASE = "https://chat.qwen.ai"
SIGNIN_PATH = "/api/v2/auths/signin"
WARM_PATH = "/auth"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

#: One mint costs ~1-3 s through the pool; the reference measured ~1.09 s/account.
DEFAULT_TTL = 6 * 86400.0
#: What a 503 tells the caller to wait. A walled egress needs minutes, and we
#: rotate, so a short hint is right here -- the next attempt is a new IP anyway.
RETRY_AFTER_SECONDS = 5


class MintError(RuntimeError):
    """Signin could not produce a token (network, pool, or refusal)."""


class WallError(MintError):
    """The egress answered with the WAF challenge page."""


# --------------------------------------------------------------- SOCKS5 dialer


def parse_socks_url(url: str) -> tuple[str, int, str, str]:
    """`socks5h://user:pass@host:port` -> (host, port, user, password)."""
    parts = urllib.parse.urlsplit(url if "://" in url else "socks5h://" + url)
    if not parts.hostname or not parts.port:
        raise ValueError("socks url needs host:port")
    return (parts.hostname, parts.port, urllib.parse.unquote(parts.username or ""),
            urllib.parse.unquote(parts.password or ""))


class Socks5Dialer:
    """Opens a **fresh** connection per call -- which is what rotates the IP.

    The pool's documented semantics: port 2088 hands out a new egress per new
    connection, while a kept-alive connection stays pinned. Never pool sockets
    here, or every signin would leave from the same address.
    """

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.host, self.port, self.user, self.password = parse_socks_url(url)
        self.timeout = timeout

    def open(self, target_host: str, target_port: int = 443) -> socket.socket:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        try:
            self._handshake(sock, target_host, target_port)
        except Exception:
            sock.close()
            raise
        return sock

    def _handshake(self, sock: socket.socket, host: str, port: int) -> None:
        methods = b"\x02" if self.user else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        chosen = self._recv(sock, 2)
        if chosen[1] == 0x02:
            user = self.user.encode()
            password = self.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user
                         + bytes([len(password)]) + password)
            if self._recv(sock, 2)[1] != 0x00:
                raise MintError("socks auth rejected")
        elif chosen[1] != 0x00:
            raise MintError(f"socks method rejected ({chosen[1]})")
        raw = host.encode()
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw
                     + struct.pack("!H", port))
        head = self._recv(sock, 4)
        if head[1] != 0x00:
            raise MintError(f"socks connect failed (code {head[1]})")
        if head[3] == 0x01:
            self._recv(sock, 4)
        elif head[3] == 0x03:
            self._recv(sock, self._recv(sock, 1)[0])
        elif head[3] == 0x04:
            self._recv(sock, 16)
        self._recv(sock, 2)

    @staticmethod
    def _recv(sock: socket.socket, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            chunk = sock.recv(count - len(buf))
            if not chunk:
                raise MintError("socks peer closed the connection")
            buf += chunk
        return buf


def http_over_socket(sock: socket.socket, host: str, method: str, path: str,
                     *, body: bytes | None = None, headers: dict | None = None,
                     timeout: float = 30.0) -> tuple[int, dict[str, list[str]], bytes]:
    """One HTTP/1.1 exchange on an already-connected socket, then close.

    `Connection: close` plus read-to-EOF keeps this honest without a chunked
    decoder: both endpoints we call answer small bodies.
    """
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=host)
    tls.settimeout(timeout)
    try:
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host}",
                 "Connection: close", f"User-Agent: {UA}"]
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")
        tls.sendall(raw)
        buf = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        tls.close()
    head, _, payload = buf.partition(b"\r\n\r\n")
    head_lines = head.decode("latin-1").split("\r\n")
    status = int(head_lines[0].split(" ")[1])
    got: dict[str, list[str]] = {}
    for line in head_lines[1:]:
        name, _, value = line.partition(":")
        got.setdefault(name.strip().lower(), []).append(value.strip())
    return status, got, payload


# ------------------------------------------------------------------ token mint


def mint_token(socks_url: str, email: str, password: str, *,
               timeout: float = 40.0, base: str = BASE) -> str:
    """Sign one account in through the pool. Raises `MintError`/`WallError`."""
    host = urllib.parse.urlsplit(base).netloc
    digest = hashlib.sha256(password.encode()).hexdigest()
    dialer = Socks5Dialer(socks_url, timeout=timeout)
    common = {"Accept": "application/json, text/plain, */*",
              "Origin": base, "Referer": base + WARM_PATH,
              "Accept-Language": "zh-CN,zh;q=0.9"}
    sock = dialer.open(host)
    try:
        # The warm-up is what earns the WAF's cold-start cookies. Through a fresh
        # pool connection it is a new browser's first visit -- which is the point.
        http_over_socket(sock, host, "GET", WARM_PATH, headers=common, timeout=timeout)
    except Exception:  # noqa: BLE001 - best effort, same as the script's
        pass
    finally:
        try:
            sock.close()
        except OSError:
            pass

    body = json.dumps({"email": email, "password": digest}).encode()
    sock = dialer.open(host)
    try:
        status, headers, payload = http_over_socket(
            sock, host, "POST", SIGNIN_PATH, body=body,
            headers={**common, "Content-Type": "application/json"},
            timeout=timeout)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    text = payload.decode("utf-8", "replace")
    if "aliyun_waf" in text:
        raise WallError("this egress is answering the WAF challenge page")
    token = ""
    for raw in headers.get("set-cookie", []):
        found = re.search(r"(?:^|[;\s])token=([^;]+)", raw)
        if found:
            token = found.group(1).strip()
    if status != 200:
        raise MintError(f"signin answered HTTP {status}")
    if not token:
        raise MintError("no token in Set-Cookie")
    return token


# ------------------------------------------------------------------ the pool


class TokenPool:
    """Per-account token cache. Mints lazily, never twice at once per account."""

    def __init__(self, accounts: dict[str, str], socks_url: str, *,
                 ttl: float = DEFAULT_TTL, mint=None) -> None:
        self.accounts = dict(accounts)
        self.socks_url = socks_url
        self.ttl = ttl
        self._tokens: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, threading.Lock] = {a: threading.Lock() for a in accounts}
        self._errors: list[str] = []
        self._mint = mint or (lambda email, password: mint_token(
            self.socks_url, email, password))
        self._mints = 0

    @property
    def mints(self) -> int:
        return self._mints

    @property
    def last_error(self) -> str | None:
        return self._errors[-1] if self._errors else None

    def depth(self) -> int:
        now = time.time()
        return sum(1 for _t, ts in self._tokens.values() if now - ts < self.ttl)

    def take(self, account: str) -> tuple[str, bool]:
        """(token, cached). Raises `MintError` when the account cannot be served."""
        if account not in self.accounts:
            raise MintError(f"unknown account {account!r}")
        lock = self._locks[account]
        with lock:
            held = self._tokens.get(account)
            if held and time.time() - held[1] < self.ttl:
                return held[0], True
            try:
                token = self._mint(account, self.accounts[account])
            except Exception as exc:  # noqa: BLE001
                self._errors.append(f"{account}: {type(exc).__name__}: {str(exc)[:160]}")
                del self._errors[:-5]
                raise
            self._mints += 1
            self._tokens[account] = (token, time.time())
            return token, False


# --------------------------------------------------------------- HTTP surface


def build_handler(pool: TokenPool, token: str = ""):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any],
                  extra: dict[str, str] | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parts = urllib.parse.urlsplit(self.path)
            if token and self.headers.get("Authorization") != "Bearer " + token:
                self._send(401, {"error": "bad or missing bearer token"})
                return
            if parts.path == "/health":
                self._send(200, {"accounts": len(pool.accounts),
                                 "tokens_cached": pool.depth(),
                                 "mints": pool.mints,
                                 "last_error": pool.last_error})
                return
            if parts.path == "/token":
                wanted = urllib.parse.parse_qs(parts.query).get("account", [""])[0]
                try:
                    minted, cached = pool.take(wanted)
                except MintError as exc:
                    # Loud and specific: the caller falls back to the guest door
                    # rather than sending a credential it does not have.
                    self._send(503, {"error": str(exc), "account": wanted},
                               extra={"Retry-After": str(RETRY_AFTER_SECONDS)})
                    return
                self._send(200, {"token": minted, "account": wanted,
                                 "cached": cached})
                return
            self._send(404, {"error": "GET /token?account=<email> or /health"})

        def log_message(self, *args):
            pass

    return Handler


def build_server(pool: TokenPool, *, host: str = "127.0.0.1", port: int = 8792,
                 token: str = "") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), build_handler(pool, token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socks", default=os.environ.get("QWEN_TOKEN_SOCKS", ""),
                        help="rotating SOCKS5 url (env QWEN_TOKEN_SOCKS); "
                             "port 2088 rotates per connection")
    parser.add_argument("--account", action="append", default=[],
                        metavar="EMAIL:PASSWORD",
                        help="account to serve (repeatable)")
    parser.add_argument("--password", default=os.environ.get("QWEN_TOKEN_PASSWORD", ""),
                        help="password for every --account that has none")
    parser.add_argument("--ttl", type=float, default=DEFAULT_TTL,
                        help="how long a token is served before re-minting")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--token", default=os.environ.get("TOKEN_SERVICE_TOKEN", ""),
                        help="require this bearer token (env TOKEN_SERVICE_TOKEN)")
    args = parser.parse_args(argv)

    accounts: dict[str, str] = {}
    for item in args.account:
        email, _, password = item.partition(":")
        accounts[email.strip()] = (password or args.password).strip()
    if not accounts:
        parser.error("at least one --account")
    if not args.socks:
        parser.error("--socks (or QWEN_TOKEN_SOCKS) is required: signing in "
                     "direct is how an egress gets walled")
    for email, password in accounts.items():
        if not password:
            parser.error(f"account {email} has no password")

    pool = TokenPool(accounts, args.socks, ttl=args.ttl)
    server = build_server(pool, host=args.host, port=args.port, token=args.token)
    print(f"[token] serving {len(accounts)} account(s) on "
          f"http://{args.host}:{args.port} (ttl={args.ttl:.0f}s, "
          f"socks={urllib.parse.urlsplit(args.socks).hostname}, "
          f"token={'set' if args.token else 'NONE'})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
