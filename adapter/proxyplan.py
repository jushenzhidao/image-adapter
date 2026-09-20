"""The channel's outbound proxy as data: which hosts, and how often to re-dial.

Two decisions live here rather than at the transport call site, because both
are about *scope* and both are easy to get subtly wrong:

* ``bypass`` -- a proxy is not a blanket "send everything through it". The case
  it exists for is a host the channel must reach **and the exit cannot**: an
  internal service (qwen's ``token_url`` is configured as a loopback address)
  is unreachable from a remote pool, which would dial *its own* 127.0.0.1 --
  so that call has to leave directly. It is a **deployment-level** list rather
  than a per-channel header, because "what must stay direct" is a property of
  the estate, not of one channel, and a per-channel list would have to be
  repeated on every channel holding the same answer. Two kinds of traffic never
  consult this list, because the channel does not choose their target: material
  downloads (the caller's URL -- ``AdapterContext.download_http``) and
  object-store uploads (the storage client carries no proxy view at all). Both
  go direct **by construction**, not because they are named here.

The plan is a frozen value object, so a channel can carry one without the
engine having to know how it was decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from adapter.errors import ChannelConfigError

#: The two wildcard shapes, and both are whole-pattern ones: ``*`` means every
#: host, ``*.example.com`` means that suffix. ``api.*.com`` is refused rather
#: than accepted and quietly never matched (see ``_is_wildcard_shape``) -- the
#: same rule, and the same reason, as the X-Model-Map catch-all.
WILDCARD = "*"

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
        if WILDCARD in pattern and not _is_wildcard_shape(pattern):
            # Refused rather than kept. `host_matches` reads a pattern it does
            # not recognise as a literal string, so `api.*.com` matches no host
            # at all -- and an entry that matches nothing reads as "this host is
            # excluded" while excluding nothing. A 400 naming the value is the
            # only honest answer, exactly as with a partial glob in X-Model-Map.
            raise ChannelConfigError(
                f"{header} supports only '*', '*.suffix' or a bare host; "
                f"{pattern!r} would never match",
                header,
            )
        patterns.append(pattern)
    return tuple(dict.fromkeys(patterns))


def _is_wildcard_shape(pattern: str) -> bool:
    """True for the only two shapes ``host_matches`` can act on.

    ``*`` is every host and ``*.suffix`` is one suffix -- that is the whole
    language. Anything else carrying a ``*`` (``api.*.com``, ``*.foo.*.com``,
    or a bare ``*.`` with nothing after it) would fall through to the literal
    comparison in ``host_matches`` and therefore match nothing at all, so
    ``parse_hosts`` refuses it instead of storing it.
    """
    if pattern == WILDCARD:
        return True
    if not pattern.startswith(f"{WILDCARD}."):
        return False
    suffix = pattern[2:]
    return bool(suffix) and WILDCARD not in suffix


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
    """Where a channel's outbound calls go."""

    url: str = ""
    #: Host patterns that stay direct (UPSTREAM_PROXY_BYPASS_HOSTS). Matched on
    #: the target's hostname only, so a pattern never depends on path or port.
    bypass: tuple[str, ...] = ()

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

