"""Optional ``manifest.json`` for a script store root.

Without a manifest a ref must name a concrete version, because the store has
nothing else to resolve against. A manifest adds two things a bare directory
cannot express:

  * **aliases** — a moving label such as ``stable`` or ``latest`` that points
    at a concrete version. Channels can say ``vendor/mj@stable`` and be moved
    forward by editing one file instead of every channel header.

    Moving one forward takes effect on the next **process start**, not on the
    next request: this file is read once, in ``DirStore.__init__``, and the store
    is built once at startup. That is the opposite of the script text itself,
    which is re-read whenever its ``(mtime_ns, size)`` changes -- so a hot-fixed
    script in an overlay directory needs no restart while an alias edit does.
    An operator who has learned the first behaviour will get this one wrong, so
    it is worth stating twice: **edit the manifest, restart the process.**

    Aliases only rewrite a ref that *has* a version. ``@v1`` is passed through
    untouched, which is what makes pinning a real escape valve -- and why a
    version must never be written as an alias for another one.
  * **digests** — the expected sha256 per version, so a root can be audited.
    A mismatch is refused rather than silently served.

``pin_digests`` controls how strict that second part is, and the default is
deliberately permissive: a manifest states intent, and an unsigned local file
must not become a hard gate that breaks a working deployment. Turn it on when
the manifest itself arrives through a trusted channel.

The file lives at ``<root>/manifest.json``:

    {
      "scripts": {
        "vendor_y/mj": {
          "latest": "v1.3",
          "aliases": {"stable": "v1.3", "canary": "v1.4"},
          "digests": {"v1.3": "abc123...", "v1.4": "def456..."}
        }
      }
    }

Absent, unreadable or malformed manifests are treated as "no manifest": the
store keeps serving concrete refs. A typo in an optional overlay file must not
take down resolution of every other ref.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from adapter.errors import ScriptSourceError
from adapter.scriptstore.ref import ParsedRef

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"

#: Bound on alias-chain following, so a cycle cannot hang a request.
MAX_ALIAS_DEPTH = 8

#: Implicit alias for the ``latest`` field, so a manifest need not repeat it
#: under ``aliases``. An explicit entry in ``aliases`` still wins.
LATEST_ALIAS = "latest"


@dataclass(frozen=True)
class ManifestEntry:
    """What the manifest knows about one script ref (the versionless path)."""

    latest: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)

    def resolve_version(self, name: str) -> str | None:
        """Follows an alias chain to a concrete version, or None if unknown.

        Returns ``name`` unchanged when it is not an alias at all, which is
        the common case: a plain concrete version needs no manifest entry.

        ``latest`` is an implicit alias for the declared ``latest`` field, so
        ``@latest`` works without also listing it under ``aliases``. When the
        two disagree the explicit ``aliases`` entry wins: a literal mapping is
        the more specific statement, and silently preferring the shorter form
        would make the alias dict unreadable as a source of truth.
        """
        seen: set[str] = set()
        current = name
        if current not in self.aliases and current == LATEST_ALIAS and self.latest:
            current = self.latest
        while current in self.aliases:
            if current in seen:
                logger.warning("manifest alias cycle detected at %r", current)
                return None
            seen.add(current)
            if len(seen) > MAX_ALIAS_DEPTH:
                logger.warning("manifest alias chain for %r is too deep", name)
                return None
            current = self.aliases[current]
        return current


class Manifest:
    """Parsed manifest for one root. Never raises on malformed content."""

    def __init__(self, entries: dict[str, ManifestEntry] | None = None) -> None:
        self._entries = entries or {}

    def __bool__(self) -> bool:
        return bool(self._entries)

    def entry_for(self, ref: ParsedRef) -> ManifestEntry | None:
        return self._entries.get("/".join(ref.parts))

    def resolve(self, ref: ParsedRef) -> ParsedRef:
        """Turns ``vendor/mj@stable`` into ``vendor/mj@v1.3`` when known.

        A ref naming a concrete version, or carrying no version, passes
        through untouched: the manifest is an *addition* to the filename
        convention, never a precondition for using it.
        """
        entry = self.entry_for(ref)
        if entry is None or not ref.version:
            return ref
        resolved = entry.resolve_version(ref.version)
        if resolved is None or resolved == ref.version:
            return ref
        return ParsedRef(raw=ref.raw, parts=ref.parts, version=resolved)

    def expected_digest(self, ref: ParsedRef) -> str | None:
        """Declared sha256 for a concrete version, when the manifest has one."""
        entry = self.entry_for(ref)
        if entry is None or not ref.version:
            return None
        return entry.digests.get(ref.version)

    def verify(self, ref: ParsedRef, text: str, *, enabled: bool) -> None:
        """Refuses text that disagrees with the manifest digest.

        Only enforced when ``pin_digests`` is on: see the module docstring for
        why the default trusts the file over the manifest.
        """
        if not enabled:
            return
        expected = self.expected_digest(ref)
        if not expected:
            return
        actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if actual != expected:
            raise ScriptSourceError(
                f"Script {ref.raw!r} does not match the manifest digest "
                f"(expected {expected[:12]}..., got {actual[:12]}...)",
                code="script_integrity_error",
            )


def _parse_aliases(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str) and v
    }


def _parse_digests(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        k: v.strip().lower()
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def parse_manifest(text: str, origin: str = MANIFEST_NAME) -> Manifest:
    """Parses manifest JSON. Any problem yields an empty manifest, not an error."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("%s is not valid JSON (%s); ignoring it", origin, exc)
        return Manifest()
    if not isinstance(data, dict):
        logger.warning("%s must be a JSON object; ignoring it", origin)
        return Manifest()
    # Accept both {"scripts": {...}} and a bare mapping of ref -> entry.
    scripts = data.get("scripts", data)
    if not isinstance(scripts, dict):
        logger.warning("%s: 'scripts' must be an object; ignoring it", origin)
        return Manifest()

    entries: dict[str, ManifestEntry] = {}
    for key, value in scripts.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        latest = value.get("latest")
        entries[key] = ManifestEntry(
            latest=latest if isinstance(latest, str) and latest else None,
            aliases=_parse_aliases(value.get("aliases")),
            digests=_parse_digests(value.get("digests")),
        )
    return Manifest(entries)


def load_manifest(root: Path) -> Manifest:
    """Reads ``<root>/manifest.json``. A missing file is not an error."""
    path = root / MANIFEST_NAME
    try:
        if not path.is_file():
            return Manifest()
        return parse_manifest(path.read_text(encoding="utf-8"), origin=str(path))
    except OSError as exc:
        logger.warning("could not read %s (%s); ignoring it", path, exc)
        return Manifest()
