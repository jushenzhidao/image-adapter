"""``X-Upstream-Proxy``: a channel's own exit, checked before it is ever used.

Each decision asserted here is tempting to reverse, which is why it is pinned:

  * **empty means "any host".** ``UPSTREAM_PROXY_ALLOWLIST`` is a
    *restriction* when it is set and no restriction when it is not -- the same
    orientation as ``X-Upstream-Url``'s list. It was fail-closed until
    2026-09-20, so the test below pins which way it goes now: a future reader
    must not be able to "restore" the old default by accident.
  * **a named list still bites.** With a value, an off-list host is refused
    before any upstream call -- a bad proxy URL hands the vendor credential to
    a stranger, so the check is worth keeping wherever it is affordable.
  * **SOCKS is refused by name.** aiohttp has no SOCKS transport, so accepting
    ``socks5://`` would mean the call quietly goes direct: a wrong-exit bug
    that looks exactly like a healthy channel.
  * **no header, no key.** A channel without the header must reach aiohttp
    with no ``proxy`` entry at all, so every existing deployment keeps the
    code path it had.
  * **the credential never reaches a trace.** ``redact_proxy`` drops userinfo,
    path and query.

The last test drives real sockets rather than inspecting kwargs: a stand-in
proxy accepts the connection and asserts it saw an absolute-URI request line,
which is what "the outbound call went through the proxy" looks like on the
wire. An assertion on ``kwargs["proxy"]`` alone would still pass if aiohttp
ignored the value.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from adapter.channel import parse_channel
from adapter.ctxapi import RequestPlan
from adapter.ctxapi.proxy_http import ProxiedHttp
from adapter.errors import ChannelConfigError
from adapter.proxyplan import ProxyPlan
from adapter.settings import Settings
from adapter.transport import build_request
from adapter.urlguard import redact_proxy

#: Lowercase because `parse_channel` reads raw header names; a plain dict in a
#: unit test is not the case-insensitive mapping the service hands it.
BASE = {
    "x-upstream-url": "https://api.vendor.test/v1/images/generations",
    "x-script-ref": "qwen/images@v1",
}


def _settings(**overrides) -> Settings:
    # `_env_file=None`: a bare Settings() reads the production .env, which would
    # make this suite depend on the machine it runs on.
    return Settings(_env_file=None, adapter_key_required=False, **overrides)


def _channel(proxy: str | None, **overrides):
    headers = dict(BASE)
    if proxy is not None:
        headers["x-upstream-proxy"] = proxy
    return parse_channel(headers, _settings(**overrides))


def test_no_header_means_no_proxy_key_at_all() -> None:
    channel = _channel(None)

    assert channel.proxy.enabled is False
    _, _, kwargs = build_request(channel, RequestPlan(), {"prompt": "a cat"}, None)
    assert "proxy" not in kwargs


def test_a_declared_proxy_is_carried_into_the_call() -> None:
    channel = _channel("http://127.0.0.1:3128", upstream_proxy_allowlist="127.0.0.1")

    _, _, kwargs = build_request(channel, RequestPlan(), {"prompt": "a cat"}, None)
    assert kwargs["proxy"] == "http://127.0.0.1:3128"


def test_an_empty_allowlist_accepts_any_host() -> None:
    """Empty is "no restriction", not "refuse the header" (inverted 2026-09-20).

    It was the other way round until then, and back then the two refusals had
    to stay tellable apart -- "this deployment turned the header off" and
    "that host is not on your list" need different fixes, while a
    key-name-only assertion passes for either. Only one refusal is left now,
    which is exactly why the direction itself is pinned here.
    """
    loopback = _channel("http://127.0.0.1:3128")
    assert loopback.proxy.url == "http://127.0.0.1:3128"

    # Not just loopback: with no list, every host is accepted.
    elsewhere = _channel("http://proxy.elsewhere.test:3128")
    assert elsewhere.proxy.url == "http://proxy.elsewhere.test:3128"


def test_a_host_outside_the_allowlist_is_refused() -> None:
    with pytest.raises(ChannelConfigError) as excinfo:
        _channel(
            "http://proxy.elsewhere.test:3128",
            upstream_proxy_allowlist="127.0.0.1,10.0.0.5",
        )
    assert "not in UPSTREAM_PROXY_ALLOWLIST" in excinfo.value.message


def test_a_wildcard_entry_is_an_explicit_opt_out() -> None:
    channel = _channel("http://proxy.internal:3128", upstream_proxy_allowlist="*")
    assert channel.proxy.url == "http://proxy.internal:3128"


def test_the_allowlist_tolerates_spacing_and_case() -> None:
    channel = _channel(
        "http://Proxy.Internal:3128", upstream_proxy_allowlist=" 127.0.0.1 , Proxy.Internal "
    )
    assert channel.proxy.url == "http://Proxy.Internal:3128"


@pytest.mark.parametrize("scheme", ["socks5", "socks5h", "socks"])
def test_socks_is_refused_by_name_rather_than_as_a_bad_scheme(scheme: str) -> None:
    """The operator has to be told the fix, not just that the value is wrong.

    "scheme must be http or https" invites retrying with a different spelling;
    the actual remedy is a local SOCKS-to-HTTP bridge, so the message says so.
    """
    with pytest.raises(ChannelConfigError) as excinfo:
        _channel(f"{scheme}://127.0.0.1:2088", upstream_proxy_allowlist="*")

    message = excinfo.value.message
    assert scheme in message
    assert "bridge" in message
    assert "http" in message


def test_a_non_http_scheme_is_refused() -> None:
    with pytest.raises(ChannelConfigError) as excinfo:
        _channel("ftp://proxy.internal:3128", upstream_proxy_allowlist="*")
    assert "scheme must be http or https" in excinfo.value.message


def test_a_hostname_is_required() -> None:
    with pytest.raises(ChannelConfigError) as excinfo:
        _channel("http://", upstream_proxy_allowlist="*")
    assert "missing a hostname" in excinfo.value.message


def test_the_trace_never_carries_the_proxy_credential() -> None:
    assert redact_proxy("http://user:sekret@127.0.0.1:3128/") == "http://127.0.0.1:3128"
    assert redact_proxy("https://pool.internal:8443") == "https://pool.internal:8443"
    assert redact_proxy("socks5h://user:pw@pool.internal:2088") == "socks5h://pool.internal:2088"
    assert redact_proxy("garbage") == "<redacted>"


async def test_the_outbound_call_really_goes_through_the_proxy() -> None:
    """Real sockets: the proxy sees an absolute-URI request line.

    The target URL is plain http on purpose. For an https target aiohttp opens
    a CONNECT tunnel first, and a stand-in proxy would have to complete a TLS
    handshake to observe anything -- the local hops are what this proves, not
    the tunnel. Port 9 is the discard port: if the request were sent directly
    it would fail, so a 200 can only come from the proxy.
    """
    lines: list[str] = []

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines.append((await reader.readline()).decode("latin-1").strip())
        while True:  # drain the request headers
            header = await reader.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        body = b'{"ok":true}'
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        headers = {
            "x-upstream-url": "http://127.0.0.1:9/v1/images/generations",
            "x-script-ref": "qwen/images@v1",
            "x-upstream-proxy": f"http://127.0.0.1:{port}",
        }
        channel = parse_channel(headers, _settings(upstream_proxy_allowlist="127.0.0.1"))
        url, method, kwargs = build_request(channel, RequestPlan(), {"prompt": "a cat"}, None)

        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, **kwargs) as response:
                assert response.status == 200
                assert await response.json() == {"ok": True}
    finally:
        server.close()
        await server.wait_closed()

    assert lines, "the proxy was never contacted"
    assert lines[0].startswith("POST http://127.0.0.1:9/v1/images/generations"), lines[0]


# --------------------------------------------------------------- scope: hosts


def _channel_with(proxy: str | None = None, **settings_overrides):
    headers = dict(BASE)
    if proxy is not None:
        headers["x-upstream-proxy"] = proxy
    return parse_channel(headers, _settings(**settings_overrides))


def test_an_object_store_upload_stays_direct() -> None:
    """UPSTREAM_PROXY_BYPASS_HOSTS: the upload hop is not what the exit is for."""
    channel = _channel_with(
        "http://127.0.0.1:3128",
        upstream_proxy_allowlist="127.0.0.1",
        upstream_proxy_bypass_hosts="*.aliyuncs.com",
    )

    _, _, upstream = build_request(channel, RequestPlan(), {"prompt": "x"}, None)
    assert upstream["proxy"] == "http://127.0.0.1:3128"

    _, _, upload = build_request(
        channel, RequestPlan(), {}, "https://oss-ap-southeast-1.aliyuncs.com/x"
    )
    assert "proxy" not in upload


def test_without_a_bypass_list_every_host_takes_the_proxy() -> None:
    channel = _channel_with("http://127.0.0.1:3128", upstream_proxy_allowlist="127.0.0.1")
    _, _, kwargs = build_request(
        channel, RequestPlan(), {}, "https://oss-ap-southeast-1.aliyuncs.com/x"
    )
    assert kwargs["proxy"] == "http://127.0.0.1:3128"


def test_a_wildcard_bypass_covers_subdomains_but_not_lookalikes() -> None:
    channel = _channel_with(
        "http://127.0.0.1:3128",
        upstream_proxy_allowlist="127.0.0.1",
        upstream_proxy_bypass_hosts="*.aliyuncs.com",
    )

    # "notaliyuncs.com" ends with the same letters and must still be proxied:
    # the dot the pattern keeps is the whole reason this is a suffix test.
    _, _, lookalike = build_request(channel, RequestPlan(), {}, "https://notaliyuncs.com/x")
    assert lookalike["proxy"] == "http://127.0.0.1:3128"

    _, _, covered = build_request(channel, RequestPlan(), {}, "https://oss.aliyuncs.com/x")
    assert "proxy" not in covered


@pytest.mark.parametrize(
    "pattern",
    ["api.*.com", "a.*.b.com", "**.foo.com", "*.foo.*.com", "*.", "x.*"],
)
def test_a_wildcard_anywhere_but_the_front_is_refused(pattern: str) -> None:
    """`host_matches` reads a pattern it does not recognise as a literal string,
    so a middle wildcard matches nothing at all.

    That is the worst shape a configuration entry can take: the operator reads it
    as "these hosts stay direct" while it excludes nothing, and no request ever
    contradicts them. It used to be *accepted* here -- the comment beside
    `WILDCARD` claimed the opposite, which is exactly how it survived review.
    Refusing it while the channel is parsed is the same rule, and the same
    reason, as a partial glob in `X-Model-Map`.
    """
    with pytest.raises(ChannelConfigError) as caught:
        _channel_with(
            "http://127.0.0.1:3128",
            upstream_proxy_allowlist="127.0.0.1",
            upstream_proxy_bypass_hosts=pattern,
        )
    assert caught.value.param == "UPSTREAM_PROXY_BYPASS_HOSTS"
    assert pattern in caught.value.message


@pytest.mark.parametrize(
    "pattern,probe",
    [
        ("*", "https://anything.example.com/x"),
        ("example.com", "https://example.com/x"),
        ("*.example.com", "https://a.example.com/x"),
    ],
)
def test_every_accepted_shape_is_one_that_can_match_something(
    pattern: str, probe: str
) -> None:
    """The positive control for the rule above.

    Three spellings are accepted and each has to be actionable: `*` and a bare
    host match by definition, and `*.suffix` needs a host underneath it. A
    refusal list is only honest if what it accepts works -- otherwise the check
    has merely moved the silent no-op to a different spelling.
    """
    channel = _channel_with(
        "http://127.0.0.1:3128",
        upstream_proxy_allowlist="127.0.0.1",
        upstream_proxy_bypass_hosts=pattern,
    )
    _, _, kwargs = build_request(channel, RequestPlan(), {}, probe)
    assert "proxy" not in kwargs, f"{pattern!r} was accepted but bypasses nothing"


def test_an_authenticated_proxy_url_is_used_as_is() -> None:
    """A proxy that authenticates by URL keeps working: nothing is echoed over it.

    The adapter adds no proxy credentials of its own -- the URL's own userinfo is
    all there is, and it is never re-emitted as a header.
    """
    channel = _channel_with(
        "http://user:pw@127.0.0.1:3128", upstream_proxy_allowlist="127.0.0.1"
    )
    _, _, kwargs = build_request(channel, RequestPlan(), {}, None)
    assert "proxy_headers" not in kwargs


# ------------------------------------------------------- the script-side view


def test_the_script_view_attaches_the_proxy_per_call() -> None:
    """`ctx.http.get(url)` must get the proxy without the script knowing.

    aiohttp takes a proxy per *request*, so this view is the only thing standing
    between a channel-level proxy and every script call going direct.
    """
    seen: list[tuple[str, dict]] = []

    class FakeSession:
        closed = False

        def get(self, url: str, **kwargs):
            seen.append((url, kwargs))
            return "context-manager"

    view = ProxiedHttp(
        FakeSession(),
        ProxyPlan(url="http://127.0.0.1:3128", bypass=("*.aliyuncs.com",)),
    )

    assert view.get("https://api.vendor.test/x") == "context-manager"
    assert seen[0][1]["proxy"] == "http://127.0.0.1:3128"

    view.get("https://oss-ap-southeast-1.aliyuncs.com/put")
    assert "proxy" not in seen[1][1]

    # Anything not a verb passes through untouched -- a script may reach for
    # `closed` or `cookie_jar`, and those must not become method wrappers.
    assert view.closed is False


