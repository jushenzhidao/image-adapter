"""The channel's outbound proxy as data: which hosts, and how often to re-dial.

Two decisions live here rather than at the transport call site, because both
are about *scope* and both are easy to get subtly wrong:

* ``bypass`` -- a proxy is not a blanket "send everything through it". An upload
  to the vendor's object store is the case this exists for: it is not the
  traffic the exit was bought for, and pushing megabytes through a pool only
  makes it slower. It is a **deployment-level** list rather than a per-channel
  header, because "what must stay direct" is a property of the estate, not of
  one channel -- and a per-channel list would have to be repeated on every
  channel that has the same answer. Reference images are excluded harder still,
  in code rather than by a list: their target is chosen by the caller, so the
  download path carries no proxy view at all (``AdapterContext.download_http``).

* ``per_request`` -- a rotating pool hands out a new address per TCP
  connection, so "one address per client request" is a statement about
  *connections*: the request must not reuse one opened for the previous
  request. ``session_id`` is what keeps a single request's calls together when
  a connection does have to be re-established -- it travels as the proxy
  credential, and the bridge on the other end pins it to one upstream tunnel.

Keeping both in a frozen value object also means the request-scoped session id
can be attached with ``dataclasses.replace`` at the one place that knows the
request boundary (the pipeline), instead of being threaded through the engine
as a separate parameter beside the channel.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlparse

from adapter.errors import ChannelConfigError

#: The only wildcard, and it is a whole-pattern one: ``*`` means every host,
#: ``*.example.com`` means that suffix. ``api.*.com`` is refused rather than
#: accepted and quietly never matched -- the same rule, and the same reason, as
#: the X-Model-Map catch-all.
WILDCARD = "*"

MODE_SHARED = "shared"
MODE_PER_REQUEST = "per-request"
MODES = frozenset({MODE_SHARED, MODE_PER_REQUEST})

#: Characters that cannot appear in a bare host pattern. A pattern carrying a
#: scheme, a port or a credential is a URL pasted into the wrong header, and it
#: would never match a hostname -- so it is refused instead of ignored.
_FORBIDDEN_IN_PATTERN = "/:@"


def parse_hosts(raw: str, header: str) -> tuple[str, ...]:
    """Comma-separated host patterns -> a deduplicated tuple. Raises on junk."""
    patterns: list[str] = []
    for item in raw.split(","):
        pattern = item.strip().lower()
        if not pattern:
            continue
        if pattern != WILDCARD and any(ch in pattern for ch in _FORBIDDEN_IN_PATTERN):
            raise ChannelConfigError(
                f"{header} entries must be bare hosts or *.suffix patterns",
                header,
            )
        patterns.append(pattern)
    return tuple(dict.fromkeys(patterns))


def host_matches(host: str, pattern: str) -> bool:
    """True when ``host`` is covered by one pattern."""
    host = host.lower()
    if pattern == WILDCARD:
        return True
    if pattern.startswith("*."):
        # ".example.com" so that "notexample.com" does not match "*.example.com".
        return host.endswith(pattern[1:])
    return host == pattern


@dataclass(frozen=True)
class ProxyPlan:
    """Where a channel's outbound calls go, and whether they may reuse a socket."""

    url: str = ""
    #: Host patterns that stay direct (UPSTREAM_PROXY_BYPASS_HOSTS). Matched on
    #: the target's hostname only, so a pattern never depends on path or port.
    bypass: tuple[str, ...] = ()
    per_request: bool = False
    session_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def covers(self, target: str) -> bool:
        """Should a call to ``target`` go through the proxy?

        Yes unless the host is on the bypass list. A target without a parseable
        host is refused the proxy rather than granted it: an empty hostname is
        a malformed URL, and the safe reading of "I cannot tell where this goes"
        is not "send it through the credential-bearing hop".

        Note that the download path never reaches here at all -- it has no
        proxy view to consult (``AdapterContext.download_http``).
        """
        if not self.url:
            return False
        host = (urlparse(target).hostname or "").lower()
        if not host:
            return False
        return not any(host_matches(host, pattern) for pattern in self.bypass)

    def credentials(self) -> tuple[str, str] | None:
        """Proxy credentials to send, or None to use the URL's own.

        Only per-request plans carry them: the session id is the whole point,
        and it has to reach the bridge on every call. A shared plan sends
        nothing extra, so a proxy that authenticates by URL keeps working.
        """
        if self.per_request and self.session_id:
            return (self.session_id, "x")
        return None

    def with_session(self, session_id: str) -> ProxyPlan:
        return replace(self, session_id=session_id)
