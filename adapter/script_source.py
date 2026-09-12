"""Resolves a ChannelSpec into verified Python source text.

Four origins, in ascending order of trust:
  inline   X-Script      literal source, dev convenience
  base64   X-Script-64   same, escape-free
  ref      X-Script-Ref  vendor_y/mj@v1.3, resolved by adapter.scriptstore
  remote   X-Script-Ref  https://... , off by default

Inline sources are remote code execution by construction. Production should
set ALLOW_INLINE_SCRIPT=false and pin SCRIPT_SHA256_ALLOWLIST.

This module owns *policy* — size caps, sha256 pinning, the digest allowlist,
SSRF and host checks — and delegates *lookup* of named refs to a pluggable
``ScriptStore`` chain. Keeping the split that way means a new storage backend
cannot accidentally bypass a guard.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from urllib.parse import urlparse

from adapter.channel import ChannelSpec
from adapter.errors import ScriptPolicyError, ScriptSourceError
from adapter.scriptstore import ChainStore, build_store, parse_ref
from adapter.settings import Settings
from adapter.trace_attrs import record_script_fallback
from adapter.urlguard import check_url


@dataclass(frozen=True)
class ScriptSource:
    text: str
    sha256: str
    origin: str
    ref: str | None = None
    #: Set only when ``ref`` named a version this deployment no longer carries
    #: and the chain served ``@stable`` instead. The caller asked for one
    #: revision and got another, which the digest alone does not say.
    fallback_to: str | None = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enforce_size(text: str, limit: int, label: str) -> None:
    size = len(text.encode("utf-8"))
    if size > limit:
        raise ScriptSourceError(
            f"{label} script is {size} bytes, over the {limit} byte limit",
            code="script_too_large",
        )


def _verify(
    source: ScriptSource, spec: ChannelSpec, settings: Settings
) -> ScriptSource:
    if spec.script_sha256 and source.sha256 != spec.script_sha256:
        raise ScriptSourceError(
            "Script does not match the X-Script-Sha256 pin",
            code="script_integrity_error",
        )
    allowlist = settings.sha256_allowlist
    if allowlist and source.sha256 not in allowlist:
        raise ScriptPolicyError("Script digest is not in SCRIPT_SHA256_ALLOWLIST")
    return source


async def resolve_source(
    spec: ChannelSpec,
    settings: Settings,
    fetch: object | None = None,
    store: ChainStore | None = None,
) -> ScriptSource:
    """Returns verified source text.

    ``fetch`` is an async (url) -> str callable used for remote refs.
    ``store`` is the ref-resolution chain; built from settings when omitted,
    though the app passes the process-wide instance built at startup.
    """
    if spec.inline_script is not None:
        if not settings.allow_inline_script:
            raise ScriptPolicyError("Inline X-Script is disabled on this deployment")
        text = spec.inline_script
        _enforce_size(text, settings.max_inline_script_bytes, "Inline")
        return _verify(ScriptSource(text, _digest(text), "inline"), spec, settings)

    if spec.script_b64 is not None:
        if not settings.allow_inline_script:
            raise ScriptPolicyError("Inline X-Script-64 is disabled on this deployment")
        try:
            raw = base64.b64decode(spec.script_b64, validate=True)
        except (binascii.Error, ValueError):
            raise ScriptSourceError("X-Script-64 is not valid base64") from None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ScriptSourceError("X-Script-64 must decode to UTF-8 text") from None
        _enforce_size(text, settings.max_inline_script_bytes, "Inline")
        return _verify(ScriptSource(text, _digest(text), "base64"), spec, settings)

    ref = spec.script_ref
    if not ref:
        raise ScriptSourceError("No script source present on the channel")

    if urlparse(ref).scheme in {"http", "https"}:
        if not settings.allow_remote_script:
            raise ScriptPolicyError(
                "Remote script refs are disabled on this deployment"
            )
        url = check_url(ref, settings, header="X-Script-Ref")
        host = (urlparse(url).hostname or "").lower()
        allowed = settings.remote_host_set
        if allowed and host not in allowed:
            raise ScriptPolicyError("Remote script host is not in REMOTE_SCRIPT_HOSTS")
        if fetch is None:
            raise ScriptSourceError("Remote script fetching is unavailable")
        text = await fetch(url)  # type: ignore[operator]
        _enforce_size(text, settings.max_script_bytes, "Remote")
        return _verify(ScriptSource(text, _digest(text), "remote", ref), spec, settings)

    backend = store if store is not None else build_store(settings)
    served = await backend.load(parse_ref(ref))
    _enforce_size(served.text, settings.max_script_bytes, "Ref")
    digest = _digest(served.text)
    # Recorded here, where the degradation is known, rather than by a caller:
    # a fact that needs a second wiring point is one that goes missing.
    fallback_to = served.served if served.degraded else None
    if fallback_to:
        record_script_fallback(
            requested=served.requested, serving=fallback_to, sha256=digest
        )
    return _verify(
        ScriptSource(served.text, digest, "ref", ref, fallback_to=fallback_to),
        spec,
        settings,
    )
