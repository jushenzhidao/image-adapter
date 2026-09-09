"""Resolves a ChannelSpec into verified Python source text.

Four origins, in ascending order of trust:
  inline   X-Script      literal source, dev convenience
  base64   X-Script-64   same, escape-free
  ref      X-Script-Ref  vendor_y/mj@v1.3, read from script_ref_dir
  remote   X-Script-Ref  https://... , off by default

Inline sources are remote code execution by construction. Production should
set ALLOW_INLINE_SCRIPT=false and pin SCRIPT_SHA256_ALLOWLIST.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from adapter.channel import ChannelSpec
from adapter.errors import ScriptPolicyError, ScriptSourceError
from adapter.settings import Settings
from adapter.urlguard import check_url

REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class ScriptSource:
    text: str
    sha256: str
    origin: str
    ref: str | None = None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enforce_size(text: str, limit: int, label: str) -> None:
    size = len(text.encode("utf-8"))
    if size > limit:
        raise ScriptSourceError(
            f"{label} script is {size} bytes, over the {limit} byte limit",
            code="script_too_large",
        )


def _verify(source: ScriptSource, spec: ChannelSpec, settings: Settings) -> ScriptSource:
    if spec.script_sha256 and source.sha256 != spec.script_sha256:
        raise ScriptSourceError(
            "Script does not match the X-Script-Sha256 pin",
            code="script_integrity_error",
        )
    allowlist = settings.sha256_allowlist
    if allowlist and source.sha256 not in allowlist:
        raise ScriptPolicyError(
            "Script digest is not in SCRIPT_SHA256_ALLOWLIST"
        )
    return source


def _resolve_ref_path(ref: str, settings: Settings) -> Path:
    """Maps vendor_y/mj@v1.3 to a file under script_ref_dir, safely."""
    body, sep, version = ref.partition("@")
    parts = [p for p in body.split("/") if p]
    if not parts or len(parts) > 4:
        raise ScriptSourceError(f"Malformed X-Script-Ref: {ref!r}")
    if sep and not version:
        raise ScriptSourceError(f"X-Script-Ref has an empty version: {ref!r}")
    for part in (*parts, version) if version else parts:
        if not REF_PATTERN.match(part) or part in {".", ".."}:
            raise ScriptSourceError(f"Illegal segment in X-Script-Ref: {ref!r}")

    root = Path(settings.script_ref_dir).resolve()
    leaf = parts[-1]
    stem = f"{leaf}@{version}" if version else leaf
    candidates = [root.joinpath(*parts[:-1], f"{stem}.py")]
    if version:
        candidates.append(root.joinpath(*parts[:-1], leaf, f"{version}.py"))

    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise ScriptSourceError(f"X-Script-Ref escapes the script store: {ref!r}")
        if resolved.is_file():
            return resolved
    raise ScriptSourceError(f"Script ref not found: {ref!r}", code="script_not_found")


async def resolve_source(
    spec: ChannelSpec,
    settings: Settings,
    fetch: object | None = None,
) -> ScriptSource:
    """Returns verified source text. `fetch` is an async (url) -> str callable."""
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
            raise ScriptPolicyError("Remote script refs are disabled on this deployment")
        url = check_url(ref, settings, header="X-Script-Ref")
        host = (urlparse(url).hostname or "").lower()
        allowed = settings.remote_host_set
        if allowed and host not in allowed:
            raise ScriptPolicyError(
                "Remote script host is not in REMOTE_SCRIPT_HOSTS"
            )
        if fetch is None:
            raise ScriptSourceError("Remote script fetching is unavailable")
        text = await fetch(url)  # type: ignore[operator]
        _enforce_size(text, settings.max_script_bytes, "Remote")
        return _verify(ScriptSource(text, _digest(text), "remote", ref), spec, settings)

    path = _resolve_ref_path(ref, settings)
    text = path.read_text(encoding="utf-8")
    _enforce_size(text, settings.max_script_bytes, "Ref")
    return _verify(ScriptSource(text, _digest(text), "ref", ref), spec, settings)
