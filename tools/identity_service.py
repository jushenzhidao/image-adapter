#!/usr/bin/env python3
"""Identity service for the qwen channel: hand out a device identity per call.

Why this exists
---------------
`chat.qwen.ai`'s guest door is not open to a server. An identity there *is* a
device fingerprint, and the drawing quota is bound to it rather than to the
exit IP (per-identity ~4-5 images/day; rotating exit IPs changes nothing --
that was measured three ways). So a guest channel keeps drawing only by holding
several identities and rotating through them, which is exactly what the qwen
script's `identity_url` channel option expects:

    GET {identity_url}  ->  200 {"cookie": "...", "bx_ua": "...", "bx_umidtoken": "..."}

This is the port of the reverse proxy's `qwen/tools/make_identities.py` and
`qwen/probe/pw_get_bxua.py`, which established the three facts it rests on
(all measured 2026-09-17):

  1. a fresh Playwright *context* yields a fresh device fingerprint, and the
     umid shows up in the page as `localStorage["lswusea"]` in the form
     ``{umid}@@{timestamp}`` (the cookie jar alone does not carry it);
  2. `bx-ua` has to be minted by the page's own anti-bot script --
     ``AWSC.configFYEx(cb, {reqUrl}, timeout)`` then ``await s.getFYToken(...)``
     -- because the token's leading number is the script version and the
     server cross-checks it against the request. The archived `fireye` copies
     in the reverse-proxy repo mint an old prefix (231!) and are therefore not
     usable for this;
  3. the context's own `chat.qwen.ai` cookies are the rest of the identity
     (x5sec / acw_tc / aui / cnaui / tfstk ...), and they only exist after the
     page has loaded.

What this is not
----------------
  - Not part of the adapter. The adapter never talks to this service except
    through `identity_url`, and the service mints identities one browser
    context at a time -- far too heavy to live in a request path.
  - Not a capacity fix. Rotating identities does not create quota; it spends
    a *different* identity's quota. The honest arithmetic, measured, is ~4-5
    drawings per identity, so the pool size is what you are willing to hold,
    and each identity still needs to be minted from a real browser session.
  - Not a credential store. Nothing is written to disk: identities live in
    the process, are retired after `--max-uses`, and the service is meant to
    be reachable only by the adapter (bind `127.0.0.1`, and set `--token`).

Retirement is an estimate, and that is deliberate
-------------------------------------------------
The service cannot see the vendor's `RateLimited`: that answer arrives to the
*adapter*, not here, and a channel option cannot report back. So an identity is
retired after `--max-uses` uses (default 4, the measured per-identity ceiling)
or after `--ttl` seconds (default 24 h, the quota's own period), whichever
comes first. Under-using an identity wastes a mint; over-using it wastes a
request that comes back 429 -- the default leans on the measured number.

Usage
-----
    pip install playwright && playwright install chrome      # once, on this host
    python tools/identity_service.py --pool 4 --port 8791 --token <secret>

    # then, on the channel:
    #   X-Channel-Options: {"chat_mode": "guest", "identity_url":
    #       "http://127.0.0.1:8791/identity?token=<secret>"}

    curl -s http://127.0.0.1:8791/health -H 'X-Identity-Token: <secret>'

    GET /identity  -> one usable identity (rotation), or 503 with the reason
    GET /health    -> {"ready", "target", "minting", "minted", "retired", "last_error"}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

HOST = "https://chat.qwen.ai"
GUEST_PATH = "/c/guest"
REQ_URL = "/api/v2/chat/completions"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

#: Minted by the page, not by us: `AWSC.configFYEx` calls back with the fy
#: object, whose `getFYToken` is the anti-bot token the request must carry.
#: The 9 s guard is the page's own contract, mirrored from the probe that
#: established it -- a token that arrives late is worse than none, because it
#: would be cross-checked against a request it was not minted for.
JS_BXUA = """
(reqUrl) => new Promise((resolve) => {
  if (!window.AWSC || !window.AWSC.configFYEx) { resolve({ok:false, err:"no AWSC"}); return; }
  let done = false;
  try {
    window.AWSC.configFYEx(function (s) {
      try {
        const t = s.getFYToken({ reqUrl: reqUrl });
        done = true;
        resolve({ ok: true, token: t });
      } catch (e) { done = true; resolve({ ok:false, err:"getFYToken: " + e.message }); }
    }, { reqUrl: reqUrl }, 8000);
  } catch (e) { resolve({ ok:false, err:"configFYEx: " + e.message }); }
  setTimeout(function () { if (!done) resolve({ok:false, err:"timeout"}); }, 9000);
})
"""

#: Every field below is required; a partial identity is not a degraded
#: identity, it is an unusable one (`_credentials` in the script refuses the
#: guest door without the two tokens, and the vendor refuses without the jar).
REQUIRED_FIELDS = ("cookie", "bx_ua", "bx_umidtoken")

#: What a 503 tells the caller to wait, in seconds. One mint costs ~7-10 s
#: (page load plus the settle window the fingerprint scripts need), so a shorter
#: hint only buys a second 503 at the same queue position.
RETRY_AFTER_SECONDS = 10


class MintError(RuntimeError):
    """A mint attempt that did not produce a complete identity."""


class PoolExhausted(RuntimeError):
    """No identity is available right now. Answered as 503, never as a stale one."""


def assemble_identity(umid_raw: Any, bxua: Any, cookies: list[dict[str, Any]]) -> dict[str, str]:
    """(cookie, bx_ua, bx_umidtoken) out of what a browser context produced.

    Split out of `BrowserMinter.mint` because this is the part with the vendor's
    shapes in it -- and therefore the part worth testing without a browser.
    Raises `MintError` on anything incomplete; see REQUIRED_FIELDS.
    """
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if "qwen.ai" in (c.get("domain") or ""))
    # `lswusea` is "{umid}@@{timestamp}"; the token is the part before it.
    umid = str(umid_raw or "").split("@@")[0].strip()
    token = ""
    if isinstance(bxua, dict) and bxua.get("ok"):
        token = str(bxua.get("token") or "").strip()

    identity = {"cookie": jar, "bx_ua": token, "bx_umidtoken": umid}
    missing = [k for k in REQUIRED_FIELDS if not identity[k]]
    if missing:
        detail = bxua.get("err") if isinstance(bxua, dict) else None
        raise MintError("incomplete identity, missing " + ", ".join(missing)
                        + ((" (" + str(detail) + ")") if detail else ""))
    return identity


# ----------------------------------------------------------------- the minter


class BrowserMinter:
    """Mints identities with a real browser: one context per identity.

    The Playwright *sync* API is thread-confined, so the whole object -- start,
    mint, close -- is used from the refill thread and from nowhere else. The
    browser process is reused across identities; only the context is new, which
    is what makes a new fingerprint (and a new umid) in the first place.
    """

    def __init__(self, *, host: str = HOST, guest_path: str = GUEST_PATH,
                 req_url: str = REQ_URL, ua: str = UA, channel: str = "chrome",
                 headless: bool = True, settle_ms: int = 7000,
                 nav_timeout_ms: int = 60000) -> None:
        self.host = host.rstrip("/")
        self.guest_path = guest_path
        self.req_url = req_url
        self.ua = ua
        self.channel = channel
        self.headless = headless
        self.settle_ms = settle_ms
        self.nav_timeout_ms = nav_timeout_ms
        self._pw = None
        self._browser = None

    # -- lifecycle (refill thread only) -----------------------------------
    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise MintError(
                "playwright is not installed on this host: "
                "`pip install playwright && playwright install "
                + self.channel + "`"
            ) from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, channel=self.channel)

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None),
                       getattr(self._pw, "stop", None)):
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - teardown must not raise
                    pass
        self._browser = None
        self._pw = None

    # -- one identity -----------------------------------------------------
    def mint(self) -> dict[str, str]:
        """(cookie, bx_ua, bx_umidtoken) from a brand-new context.

        Raises MintError on anything incomplete. It never falls back to a
        cached or partial identity: a stale identity silently burns the little
        quota it has left, which is the failure mode this service exists to
        avoid.
        """
        self._ensure_browser()
        ctx = self._browser.new_context(user_agent=self.ua, locale="zh-CN")
        try:
            page = ctx.new_page()
            page.goto(self.host + self.guest_path,
                      wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
            # The fingerprint scripts are injected by the page and need a
            # moment; the probe this is ported from waited the same 7 s.
            page.wait_for_timeout(self.settle_ms)
            umid_raw = page.evaluate("() => localStorage.getItem('lswusea') || ''")
            bxua = page.evaluate(JS_BXUA, self.req_url)
            cookies = ctx.cookies()
        finally:
            ctx.close()

        return assemble_identity(umid_raw, bxua, cookies)


# ------------------------------------------------------------------- the pool


class IdentityPool:
    """A rotating set of identities, refilled in the background.

    `mint` is injected so the pool -- the part with the policy in it -- can be
    tested without a browser, which is also how the tests run in CI.
    """

    def __init__(self, mint: Callable[[], dict[str, str]], *, target: int = 4,
                 max_uses: int = 4, ttl: float = 86400.0,
                 refill_interval: float = 1.0, clock: Callable[[], float] = time.time,
                 on_error: Callable[[BaseException], None] | None = None) -> None:
        self._mint = mint
        self.target = max(1, int(target))
        self.max_uses = max(1, int(max_uses))
        self.ttl = float(ttl)
        self.refill_interval = float(refill_interval)
        self._clock = clock
        self._on_error = on_error
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._minting = False
        self.minted = 0
        self.retired = 0
        self.last_error: str | None = None

    # -- policy (the part worth testing) ----------------------------------
    def _live_locked(self) -> list[dict[str, Any]]:
        """Drop what is used up or too old, and count it as retired."""
        now = self._clock()
        keep = [it for it in self._items
                if it["uses"] < self.max_uses and now - it["born"] < self.ttl]
        self.retired += len(self._items) - len(keep)
        self._items = keep
        return keep

    def take(self) -> dict[str, str]:
        """The next identity, rotated. Raises PoolExhausted when there is none.

        Rotation is round-robin over what is live: the point is that
        consecutive requests do not spend the same identity's quota, not that
        every request gets an identity nobody has ever used.
        """
        with self._lock:
            live = self._live_locked()
            if not live:
                raise PoolExhausted(
                    "no usable identity" + (": " + self.last_error
                                            if self.last_error else ""))
            item = live[0]
            item["uses"] += 1
            self._items = self._items[1:] + self._items[:1]   # rotate
            return dict(item["identity"])

    def stock(self, identity: dict[str, str]) -> None:
        """Add a minted identity (used by the refill loop and by tests)."""
        with self._lock:
            self._items.append({"identity": identity, "uses": 0,
                                "born": self._clock()})
            self.minted += 1

    def depth(self) -> int:
        with self._lock:
            return len(self._live_locked())

    def snapshot(self) -> dict[str, Any]:
        return {"ready": self.depth(), "target": self.target,
                "minting": self._minting, "minted": self.minted,
                "retired": self.retired, "max_uses": self.max_uses,
                "ttl": self.ttl, "last_error": self.last_error}

    # -- background refill ------------------------------------------------
    def _refill_once(self) -> None:
        while not self._stop.is_set() and self.depth() < self.target:
            self._minting = True
            try:
                self.stock(self._mint())
                self.last_error = None
            except BaseException as exc:  # noqa: BLE001 - a bad mint must not
                # kill the loop: the next attempt may well succeed, and the
                # reason has to stay visible on /health meanwhile.
                self.last_error = type(exc).__name__ + ": " + str(exc)[:200]
                if self._on_error is not None:
                    self._on_error(exc)
                return
            finally:
                self._minting = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._refill_once()
            self._stop.wait(self.refill_interval)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="identity-refill",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


# ------------------------------------------------------------------ readiness


def warm_up(pool: IdentityPool, *, want: int = 1, timeout: float = 90.0,
            interval: float = 0.5) -> bool:
    """Block until the pool holds ``want`` identities, or give up.

    Without this the first guest request after a restart is answered 503: the
    pool fills in the background and one mint costs ~7-10 s, so a channel could
    be told "no usable identity" while identities were already on their way.
    Waiting happens **once, before the listener is up**, so it costs nothing at
    runtime -- and it turns "the first requests fail" into "ready means ready".

    False means the deadline passed; the caller serves anyway, because a 503
    that names its reason beats refusing to start. Playwright's sync API is
    thread-confined, so this only polls ``depth()``; the minting stays where it
    belongs, on the refill thread.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    wanted = max(1, want)
    while time.monotonic() < deadline:
        if pool.depth() >= wanted:
            return True
        time.sleep(max(0.01, interval))
    return pool.depth() >= wanted


# --------------------------------------------------------------- HTTP surface


def build_handler(pool: IdentityPool, token: str = "") -> type[BaseHTTPRequestHandler]:
    """The two endpoints the script and an operator need, and nothing else."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "identity-service/1"

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

        def _authorised(self, query: dict[str, list[str]]) -> bool:
            """Token via header or `?token=` -- the script can only send a URL,
            so the query form is the one that matters in practice."""
            if not token:
                return True
            offered = (self.headers.get("X-Identity-Token")
                       or (query.get("token") or [""])[0])
            return offered == token

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if not self._authorised(query):
                self._send(401, {"error": "missing or wrong identity token"})
                return
            if parts.path == "/health":
                self._send(200, pool.snapshot())
                return
            if parts.path == "/identity":
                try:
                    identity = pool.take()
                except PoolExhausted as exc:
                    # Loud, and specific: an adapter that gets a stale identity
                    # instead would spend quota it does not have. `Retry-After`
                    # is the machine-readable half of the same sentence.
                    self._send(503, {"error": str(exc)},
                               extra={"Retry-After": str(RETRY_AFTER_SECONDS)})
                    return
                self._send(200, identity)
                return
            self._send(404, {"error": "no such endpoint", "path": parts.path})

        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("IDENTITY_SERVICE_QUIET"):
                return
            sys.stderr.write("[identity] " + fmt % args + "\n")

    return Handler


def build_server(pool: IdentityPool, *, host: str = "127.0.0.1", port: int = 8791,
                 token: str = "") -> ThreadingHTTPServer:
    """A bound-but-not-serving HTTP server, so tests can use an ephemeral port."""
    return ThreadingHTTPServer((host, port), build_handler(pool, token))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--token", default=os.environ.get("IDENTITY_TOKEN", ""),
                        help="require this token on every call (env IDENTITY_TOKEN)")
    parser.add_argument("--pool", type=int, default=4,
                        help="how many identities to hold ready")
    parser.add_argument("--max-uses", type=int, default=4,
                        help="retire an identity after this many handouts "
                             "(measured per-identity ceiling is 4-5 images/day)")
    parser.add_argument("--ttl", type=float, default=86400.0,
                        help="retire an identity this many seconds after minting")
    parser.add_argument("--channel", default="chrome",
                        help="Playwright browser channel (chrome|chromium)")
    parser.add_argument("--headful", action="store_true",
                        help="run with a visible browser (debugging only)")
    parser.add_argument("--settle-ms", type=int, default=7000,
                        help="how long to let the page's fingerprint scripts run")
    parser.add_argument("--warmup", type=int, default=1, metavar="N",
                        help="wait for N identities before listening "
                             "(0 = serve immediately and let callers see 503)")
    parser.add_argument("--warmup-timeout", type=float, default=90.0,
                        help="give up waiting after this many seconds and serve anyway")
    args = parser.parse_args(argv)

    minter = BrowserMinter(channel=args.channel, headless=not args.headful,
                           settle_ms=args.settle_ms)
    errors: list[str] = []

    def note(exc: BaseException) -> None:
        errors.append(type(exc).__name__ + ": " + str(exc)[:200])
        del errors[:-5]
        sys.stderr.write("[identity] mint failed: " + errors[-1] + "\n")

    pool = IdentityPool(minter.mint, target=args.pool, max_uses=args.max_uses,
                        ttl=args.ttl, on_error=note)
    server = build_server(pool, host=args.host, port=args.port, token=args.token)
    pool.start()
    if args.warmup > 0:
        want = max(1, min(args.warmup, args.pool))
        sys.stderr.write(f"[identity] warming up ({want} identity(ies); each mint takes "
                         f"~7-10s)...\n")
        if warm_up(pool, want=want, timeout=args.warmup_timeout):
            sys.stderr.write(f"[identity] ready with {pool.depth()} identity(ies)\n")
        else:
            sys.stderr.write(
                "[identity] WARNING: warm-up timed out after "
                f"{args.warmup_timeout:.0f}s; serving anyway, so requests get 503 "
                f"until the pool fills (last error: {pool.last_error})\n")
    sys.stderr.write(
        f"[identity] listening on http://{args.host}:{args.port} "
        f"(pool={args.pool}, max_uses={args.max_uses}, token={'set' if args.token else 'NONE'})\n")
    if not args.token and args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write(
            "[identity] WARNING: bound to a routable address with no token -- "
            "this endpoint hands out live credentials to anyone who can reach it\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pool.stop()
        minter.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
