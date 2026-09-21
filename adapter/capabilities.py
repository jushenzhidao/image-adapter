"""Upstream capability facts, loaded from data instead of baked into scripts.

A capability table is *measured*, not authored: tiers, ratios and which spellings a
gateway accepts are facts about someone else's service. Keeping them inside a script
meant that calibrating one fact cost a script edit, a manifest re-hash and a release
-- which happened three times in a single day while bringing up the Gemini channel.

Layout::

    <root>/capabilities/<vendor>.json

Roots are searched in the same order the script store uses -- overlay dirs first,
then the image store -- so a deployment can hot-fix a table from a mounted volume
without rebuilding anything.

Loading is deliberately forgiving, for the same reason ``manifest.json`` is: a
missing or malformed table yields "no facts", and the caller decides what to do
without them. A data file must never be able to take down a request, and a script
must never guess from a half-parsed table.

Scripts reach this only through ``ctx.caps()``: the sandbox's import whitelist keeps
them away from adapter internals, which is also what keeps this a *data* channel
rather than an API surface scripts can grow to depend on.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: One cache slot per file, reused across requests in this worker. Keyed by the
#: resolved path, validated by (mtime_ns, size) -- the same cheap check DirStore
#: uses, so a hot-fixed table is picked up without a restart.
_CACHE: dict[Path, tuple[tuple[int, int], dict | None]] = {}


def _read(path: Path) -> dict | None:
    """Parses one table file. Anything unreadable is 'no facts', never an error."""
    try:
        stat = path.stat()
    except OSError:
        # Absent (or unreadable) is not cached: a table mounted later must be picked
        # up on the next request, and stat() is cheap enough to pay every time.
        return None

    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = data if isinstance(data, dict) else None
        if parsed is None:
            logger.warning("capability table %s must be a JSON object; ignoring it", path)
    except (OSError, ValueError) as exc:
        logger.warning("could not read capability table %s (%s); ignoring it", path, exc)
        parsed = None

    _CACHE[path] = (stamp, parsed)
    return parsed


def _resolve(table: dict, model: str) -> str:
    """Applies aliases, and nothing else.

    The table rewrites a name only where it says so: an entry under ``aliases``.
    Everything else is the control plane's own vocabulary. A spelling the table
    does not have is an unknown model rather than a "near" id to be guessed at,
    and reaching a vendor under a different spelling is the channel's own call
    (``X-Model-Map``), not the loader's.
    """
    name = model.strip()
    aliases = table.get("aliases")
    if isinstance(aliases, dict):
        name = str(aliases.get(name, name))
    return name


def lookup(vendor: str, model: str | None, roots: tuple[Path, ...]) -> dict | None:
    """Returns the resolved capability facts for one model, or None.

    ``None`` means "we have no facts", which is a state the caller must handle
    explicitly rather than a reason to invent defaults: a script that cannot read
    the table should send less, not guess more.
    """
    table = None
    for root in roots:
        candidate = _read(root / f"{vendor}.json")
        if candidate is not None:
            table = candidate
            break
    if table is None:
        return None

    models = table.get("models")
    if not isinstance(models, dict):
        return None

    name = _resolve(table, model) if model and model.strip() else ""
    if not name:
        default = table.get("default_model")
        name = str(default) if isinstance(default, str) and default else ""
    caps = models.get(name)
    if not isinstance(caps, dict):
        return None
    # Vendor-wide facts (a ratio list shared by every model of the family) live under
    # "shared" and are merged in, so a script reads one flat dict and the table does
    # not repeat the same list per model.
    shared = table.get("shared")
    facts = {**(shared if isinstance(shared, dict) else {}), **caps}
    # The resolved id travels with the facts: a caller that must write the model
    # into a URL needs the normalised name, not the one the client happened to send.
    return {"model": name, **facts}


#: Facts that belong to **every** vendor rather than one of them. Today that is the
#: natural-language vocabulary of a safety refusal: the phrasing is ordinary Chinese
#: ("内容安全警告…"), not a vendor's private string, and every channel must recognise
#: the same one -- a per-vendor copy would drift exactly where uniformity matters.
SHARED_TABLE = "_shared.json"


def shared_facts(roots: tuple[Path, ...]) -> dict:
    """The cross-vendor table (``capabilities/_shared.json``), or ``{}``.

    Same forgiving contract as ``lookup``: absent or malformed is "no facts", never
    an error, and the same mtime-validated cache means a hot-fix is picked up without
    a restart. Roots follow ``capability_roots`` order, so an overlay can override
    the shipped table.
    """
    for root in roots:
        table = _read(root / SHARED_TABLE)
        if table is not None:
            return table
    return {}


def known_models(vendor: str, roots: tuple[Path, ...]) -> tuple[str, ...]:
    """Every model id with facts in this vendor's table (empty when there are none)."""
    for root in roots:
        table = _read(root / f"{vendor}.json")
        if table is not None:
            models = table.get("models")
            if isinstance(models, dict):
                return tuple(sorted(str(k) for k in models))
    return ()


def clear_cache() -> None:
    """Drops the parse cache. Tests use it; nothing in the request path does."""
    _CACHE.clear()
