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
