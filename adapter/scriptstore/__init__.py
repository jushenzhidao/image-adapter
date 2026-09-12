"""Pluggable script stores behind ``X-Script-Ref``.

Layout of the concern:

    ref.py        ref grammar, shared by all backends
    manifest.py   optional per-root manifest.json (aliases + digests)
    base.py       ScriptStore protocol + ChainStore precedence
    dirstore.py   filesystem backend, one instance per root

Deployment shapes, both supported at once and resolved in this order:

    1. SCRIPT_OVERLAY_DIRS   optional, comma-separated. Read-only volume
                             mounts. Highest precedence, so an operator can
                             ship a script fix without rebuilding the image.
    2. SCRIPT_REF_DIR        the image-baked ``/app/script_store``. Always
                             present, so refs resolve with no host dependency.

The default therefore stays "read from the image": with no overlay configured
the chain is exactly one backend and behaves as it always did. Because
``ScriptCache`` is keyed on the sha256 of the source text, a changed file in a
mounted overlay is a different cache key and takes effect without a restart.

A root may also carry a ``manifest.json`` giving aliases (``@stable`` ->
``@v1.3``) and expected digests. It is strictly optional: without one, refs
must name a concrete version. An unknown alias simply passes through, so
adding a manifest can never break a deployment that does not have one.

Adding a backend (git, S3, an HTTP catalogue) means writing one class with
``name`` and ``async get(ref)`` and inserting it in ``build_store``; no caller
changes.
"""

from __future__ import annotations

import logging

from adapter.scriptstore.base import ChainStore, ScriptStore, ServedScript
from adapter.scriptstore.dirstore import DirStore
from adapter.scriptstore.manifest import (
    MANIFEST_NAME,
    STABLE_ALIAS,
    Manifest,
    ManifestEntry,
    load_manifest,
    parse_manifest,
)
from adapter.scriptstore.ref import REF_PATTERN, ParsedRef, parse_ref
from adapter.settings import Settings

logger = logging.getLogger(__name__)


def build_store(settings: Settings) -> ChainStore:
    """Assembles the ref-resolution chain: overlays first, image root last."""
    pin = settings.script_pin_manifest_digests
    roots: list[DirStore] = [
        DirStore(path, label="overlay", pin_digests=pin)
        for path in settings.script_overlay_list
    ]
    roots.append(DirStore(settings.script_ref_dir, label="image", pin_digests=pin))
    _warn_about_missing_stable(roots)
    return ChainStore(roots)


def _warn_about_missing_stable(roots: list[DirStore]) -> None:
    """Every manifest entry should define ``stable``.

    The chain degrades a retired version to ``@stable``, so an entry without it
    turns that degradation back into a hard failure. Reported here -- once per
    root, at startup, where an operator can act on it -- rather than on
    whichever request happens to discover it.
    """
    for store in roots:
        for ref, entry in store.manifest.items():
            if not entry.defines(STABLE_ALIAS):
                logger.warning(
                    "%s: manifest entry %r defines no %r alias, so a retired "
                    "version of it will fail instead of degrading",
                    store.name,
                    ref,
                    STABLE_ALIAS,
                )


__all__ = [
    "MANIFEST_NAME",
    "REF_PATTERN",
    "STABLE_ALIAS",
    "ChainStore",
    "DirStore",
    "Manifest",
    "ManifestEntry",
    "ParsedRef",
    "ScriptStore",
    "ServedScript",
    "build_store",
    "load_manifest",
    "parse_manifest",
    "parse_ref",
]
