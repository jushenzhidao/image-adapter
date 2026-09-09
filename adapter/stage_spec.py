"""Cascade directives carried on request headers (v1.1).

Split out of channel.py to keep that module within the per-file size budget.
These three headers are all optional: a script that declares STAGES runs fine
with none of them, reusing the channel URL for every stage.

  X-Stages         generate,upscale
  X-Stage-Urls     generate=https://a.test/t2i,upscale=https://b.test/sr
  X-Stage-Timeout  total=300
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from adapter.errors import ChannelConfigError
from adapter.settings import Settings
from adapter.urlguard import check_url

H_STAGES = "x-stages"
H_STAGE_URLS = "x-stage-urls"
H_STAGE_TIMEOUT = "x-stage-timeout"

# Stage names reach phase strings and span attributes, so keep them to a
# boring alphabet rather than accepting arbitrary header text.
STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class StageSpec:
    """Control-plane overrides for a cascade."""

    names: tuple[str, ...] = ()
    urls: dict[str, str] = field(default_factory=dict)
    budget: float | None = None

    @classmethod
    def parse(cls, headers, settings: Settings) -> StageSpec:
        names = _parse_names(headers.get(H_STAGES))
        urls = _parse_urls(headers.get(H_STAGE_URLS), settings)
        budget = _parse_budget(headers.get(H_STAGE_TIMEOUT))
        # A URL keyed to a stage the caller also enumerated but misspelled is
        # a silent misroute, so require the keys to be a subset.
        if names and urls:
            unknown = sorted(set(urls) - set(names))
            if unknown:
                raise ChannelConfigError(
                    f"X-Stage-Urls names stages absent from X-Stages: {unknown}",
                    "X-Stage-Urls",
                )
        return cls(names=names, urls=urls, budget=budget)


def _parse_names(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return ()
    for name in names:
        if not STAGE_NAME_RE.match(name):
            raise ChannelConfigError(
                f"Stage name {name!r} must match [A-Za-z0-9_-]+", "X-Stages"
            )
    if len(set(names)) != len(names):
        raise ChannelConfigError("X-Stages contains duplicate names", "X-Stages")
    return tuple(names)


def _parse_urls(raw: str | None, settings: Settings) -> dict[str, str]:
    """Parses name=url pairs, each URL through the same SSRF guard."""
    if not raw or not raw.strip():
        return {}
    urls: dict[str, str] = {}
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        name, sep, value = token.partition("=")
        name = name.strip()
        if not sep or not name or not value.strip():
            raise ChannelConfigError(
                "X-Stage-Urls must use name=url pairs, e.g. "
                "generate=https://a.test/t2i",
                "X-Stage-Urls",
            )
        if not STAGE_NAME_RE.match(name):
            raise ChannelConfigError(
                f"Stage name {name!r} must match [A-Za-z0-9_-]+", "X-Stage-Urls"
            )
        if name in urls:
            raise ChannelConfigError(
                f"X-Stage-Urls repeats stage {name!r}", "X-Stage-Urls"
            )
        # Same guard as X-Upstream-Url: these are caller-supplied.
        urls[name] = check_url(value.strip(), settings, header="X-Stage-Urls")
    return urls


def _parse_budget(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if key != "total":
            raise ChannelConfigError(
                f"Unknown X-Stage-Timeout key {key!r}; only 'total' is supported",
                "X-Stage-Timeout",
            )
        try:
            parsed = float(value.strip())
        except ValueError:
            raise ChannelConfigError(
                "X-Stage-Timeout total must be numeric", "X-Stage-Timeout"
            ) from None
        if parsed <= 0:
            raise ChannelConfigError(
                "X-Stage-Timeout total must be positive", "X-Stage-Timeout"
            )
        return parsed
    return None
