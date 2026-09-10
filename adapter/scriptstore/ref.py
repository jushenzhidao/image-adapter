"""Named-ref parsing, shared by every store backend.

A ref looks like ``vendor_y/mj@v1.3``: up to four path-ish segments and an
optional version. Parsing is separated from lookup so a new backend (git, S3,
a database) inherits the exact same grammar and the same rejection rules
instead of re-implementing them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from adapter.errors import ScriptSourceError

REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

MAX_SEGMENTS = 4


@dataclass(frozen=True)
class ParsedRef:
    """A validated ref: path segments plus an optional version tag."""

    raw: str
    parts: tuple[str, ...]
    version: str | None

    @property
    def leaf(self) -> str:
        return self.parts[-1]

    @property
    def stem(self) -> str:
        """Filename stem for the flat layout: ``mj@v1.3`` or ``mj``."""
        return f"{self.leaf}@{self.version}" if self.version else self.leaf


def parse_ref(ref: str) -> ParsedRef:
    """Validates a ref. Raises ScriptSourceError on anything malformed.

    The segment allowlist is what makes path traversal impossible before a
    backend ever touches the filesystem: ``.`` and ``..`` cannot match
    REF_PATTERN, and neither can a separator or a NUL byte.
    """
    body, sep, version = ref.partition("@")
    parts = [p for p in body.split("/") if p]
    if not parts or len(parts) > MAX_SEGMENTS:
        raise ScriptSourceError(f"Malformed X-Script-Ref: {ref!r}")
    if sep and not version:
        raise ScriptSourceError(f"X-Script-Ref has an empty version: {ref!r}")
    for part in (*parts, version) if version else parts:
        if not REF_PATTERN.match(part) or part in {".", ".."}:
            raise ScriptSourceError(f"Illegal segment in X-Script-Ref: {ref!r}")
    return ParsedRef(raw=ref, parts=tuple(parts), version=version or None)
