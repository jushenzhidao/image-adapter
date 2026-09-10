"""Filesystem backend: a ref resolves to a file under one root directory.

Two layouts are accepted per root, checked in this order:

    <root>/vendor_y/mj@v1.3.py      flat, version in the filename
    <root>/vendor_y/mj/v1.3.py      nested, version as the filename

A root may also carry an optional ``manifest.json`` (see ``manifest.py``) that
maps aliases such as ``stable`` onto concrete versions. Alias resolution runs
before the filesystem is touched, so the rest of this class only ever deals
with concrete versions.

A root that does not exist is not an error: that is exactly the state of an
optional overlay directory before anyone mounts one, and the chain simply
moves on to the next backend.
"""

from __future__ import annotations

from pathlib import Path

from adapter.errors import ScriptSourceError
from adapter.scriptstore.manifest import Manifest, load_manifest
from adapter.scriptstore.ref import ParsedRef


class DirStore:
    """Reads script text from a single root directory."""

    def __init__(
        self,
        root: str | Path,
        label: str = "dir",
        *,
        pin_digests: bool = False,
    ) -> None:
        # Resolved once at construction: the containment check below compares
        # against this, so a symlinked root stays consistent per process.
        self.root = Path(root).resolve()
        self.name = f"{label}:{self.root}"
        self._pin_digests = pin_digests
        # Loaded once, like the root: both are deployment-time facts. A
        # malformed manifest yields an empty one rather than an error.
        self.manifest: Manifest = load_manifest(self.root)

    def candidates(self, ref: ParsedRef) -> list[Path]:
        parents = ref.parts[:-1]
        paths = [self.root.joinpath(*parents, f"{ref.stem}.py")]
        if ref.version:
            paths.append(self.root.joinpath(*parents, ref.leaf, f"{ref.version}.py"))
        return paths

    def _read(self, ref: ParsedRef, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ScriptSourceError(
                f"Script {ref.raw!r} is not valid UTF-8 text",
                code="script_decode_error",
            ) from None
        except OSError as exc:
            # Present but unreadable is a real failure: falling through would
            # silently serve a different revision of the script.
            raise ScriptSourceError(
                f"Script {ref.raw!r} could not be read: {exc.strerror}",
                code="script_unreadable",
            ) from exc

    async def get(self, ref: ParsedRef) -> str | None:
        if not self.root.is_dir():
            return None
        # Aliases first: `vendor/mj@stable` becomes `vendor/mj@v1.3` using this
        # root's manifest. An unknown alias passes through unchanged — it may
        # still be a real version, or another root's alias.
        concrete = self.manifest.resolve(ref)
        for candidate in self.candidates(concrete):
            resolved = candidate.resolve()
            # Defence in depth: parse_ref already rejects traversal segments,
            # but a symlink inside the root could still point outside it.
            if not resolved.is_relative_to(self.root):
                raise ScriptSourceError(
                    f"X-Script-Ref escapes the script store: {ref.raw!r}"
                )
            if resolved.is_file():
                text = self._read(concrete, resolved)
                self.manifest.verify(concrete, text, enabled=self._pin_digests)
                return text
        return None
