"""SSRF guard for caller-supplied URLs.

X-Upstream-Url arrives in a header, so it is untrusted input even when the
control plane is trusted. Every outbound target passes through check_url()
before a socket is opened.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from adapter.errors import ChannelConfigError
from adapter.settings import Settings

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that resolve to infrastructure metadata endpoints.
BLOCKED_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url(raw: str, settings: Settings, header: str = "X-Upstream-Url") -> str:
    """Validates an outbound URL. Returns it unchanged, or raises."""
    if not raw or not raw.strip():
        raise ChannelConfigError(f"{header} must not be empty", header)

    url = raw.strip()
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ChannelConfigError(
            f"{header} scheme must be http or https", header
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise ChannelConfigError(f"{header} is missing a hostname", header)
    if host in BLOCKED_HOSTS:
        raise ChannelConfigError(f"{header} host is not permitted", header)

    allowlist = settings.upstream_host_set
    if allowlist and host not in allowlist:
        raise ChannelConfigError(
            f"{header} host is not in the configured allowlist", header
        )

    if not settings.upstream_allow_private_network:
        if host in {"localhost", "localhost.localdomain"} or _is_private_ip(host):
            raise ChannelConfigError(
                f"{header} points at a private or loopback address", header
            )
        # 169.254.169.254 style metadata IPs are covered by _is_private_ip via
        # is_link_local; hostname-based metadata endpoints are in BLOCKED_HOSTS.

    return url


#: Schemes a caller may *name* but the adapter cannot use. Kept apart from
#: ALLOWED_SCHEMES because they need their own message: "scheme must be http or
#: https" does not tell the operator that the fix is to run a bridge, and the
#: failure mode being prevented is not a 400 but a silent direct connection.
SOCKS_SCHEMES = frozenset({"socks", "socks4", "socks4a", "socks5", "socks5h"})


def check_proxy_url(
    raw: str, settings: Settings, header: str = "X-Upstream-Proxy"
) -> str:
    """Validates an outbound *proxy* URL from a header. Returns it, or raises.

    Deliberately not ``check_url``, in three ways that matter:

    * the upstream allowlist does not apply -- a proxy is not an upstream --
      and this header has its own list whose **empty default refuses every
      value** instead of allowing any. The asymmetry with ``check_url`` is the
      point: a bad upstream URL reaches an attacker's server with a request,
      while a bad proxy URL hands it the credential and the whole body.
    * loopback and private hosts are allowed here (``check_url`` refuses them
      unless ``UPSTREAM_ALLOW_PRIVATE_NETWORK`` is on). A bridge listening on
      127.0.0.1 is the deployment this exists for, and the allowlist is what
      gates it, not the IP range.
    * SOCKS is refused by name. aiohttp has no SOCKS transport, so accepting
      the scheme would mean the call quietly goes direct -- a wrong-exit bug
      that looks exactly like a working channel.
    """
    if not raw or not raw.strip():
        raise ChannelConfigError(f"{header} must not be empty", header)

    url = raw.strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in SOCKS_SCHEMES:
        raise ChannelConfigError(
            f"{header} cannot use {scheme}://: the adapter's HTTP client speaks "
            "no SOCKS. Run a local SOCKS-to-HTTP bridge next to the adapter and "
            "point this header at the bridge (http://127.0.0.1:<port>)",
            header,
        )
    if scheme not in ALLOWED_SCHEMES:
        raise ChannelConfigError(f"{header} scheme must be http or https", header)

    host = (parsed.hostname or "").lower()
    if not host:
        raise ChannelConfigError(f"{header} is missing a hostname", header)

    allowed = settings.upstream_proxy_host_set
    if not allowed:
        raise ChannelConfigError(
            f"{header} is disabled on this deployment: set "
            "UPSTREAM_PROXY_ALLOWLIST to the proxy hosts it trusts "
            "(comma-separated, or * to allow any host)",
            header,
        )
    if "*" not in allowed and host not in allowed:
        raise ChannelConfigError(
            f"{header} host is not in UPSTREAM_PROXY_ALLOWLIST", header
        )

    return url


def redact_proxy(url: str) -> str:
    """``http://user:secret@host:3128/`` -> ``http://host:3128``.

    Every place that records the proxy -- traces, span attributes, error text
    -- goes through this. The credential in a proxy URL authenticates this
    service to the proxy, so it is as sensitive as the upstream key the proxy
    is trusted with, and a trace is a place credentials leak by accident.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return "<redacted>"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
