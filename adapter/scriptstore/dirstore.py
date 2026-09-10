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

Reads are cached against the file's ``(mtime_ns, size)``. Those two numbers
are the entire reason the cache is here: a stat costs 0.0026 ms where the read
costs 3.22 ms, so without it every request -- not just every cache miss -- pays
a filesystem round-trip for a file that changes once a deployment.
"""

from __future__ import annotations

from pathlib import Path
from stat import S_ISREG

from adapter.errors import ScriptSourceError

#: Upper bound on cached sources per root. Only files that actually exist are
#: ever cached, so in practice this is bounded by the size of the script store;
#: the cap is here so a caller inventing refs cannot grow the map.
_MAX_CACHED = 128
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
        # concrete ref -> (mtime_ns, size, text)
        self._cache: dict[str, tuple[int, int, str]] = {}

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

    def _read_if_file(self, ref: ParsedRef, path: Path) -> str | None:
        """Returns a regular file's text, or None when there is no file there.

        One stat does the work that used to take an ``is_file()`` plus a read,
        and it is the reason the cache exists: a stat costs 0.0026 ms where the
        read costs 3.22 ms, so without it *every* request -- not merely every
        cache miss -- pays a filesystem round-trip for a file that changes once
        a deployment.

        Both mtime and size are compared on purpose. A cache that swallowed an
        edit would break the hot-patching the overlay roots exist for, and mtime
        alone misses that on a filesystem with coarse timestamps.
        """
        try:
            info = path.stat()
        except OSError:
            return None
        if not S_ISREG(info.st_mode):
            # A directory at this path is not a candidate, which is what the
            # is_file() check used to decide.
            return None

        cached = self._cache.get(ref.raw)
        if (
            cached is not None
            and cached[0] == info.st_mtime_ns
            and cached[1] == info.st_size
        ):
            return cached[2]

        text = self._read(ref, path)
        if len(self._cache) >= _MAX_CACHED:
            self._cache.clear()
        self._cache[ref.raw] = (info.st_mtime_ns, info.st_size, text)
        return text

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
            text = self._read_if_file(concrete, resolved)
            if text is not None:
                self.manifest.verify(concrete, text, enabled=self._pin_digests)
                return text
        return None
