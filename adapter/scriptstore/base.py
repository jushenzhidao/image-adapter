"""The ScriptStore contract and the chain that composes backends.

A backend answers one question: given a validated ref, what is the script
text? Everything policy-related (size caps, sha256 pinning, allowlists) stays
in ``adapter.script_source``, so a new backend cannot accidentally weaken it.

``ChainStore`` is what makes the image-baked and volume-mounted layouts
coexist: it walks backends in order and returns the first hit, treating
"absent here" (``None``) as distinct from "broken here" (an exception). That
distinction is the whole point — a missing file must fall through to the next
root, while an unreadable file must surface as an error rather than silently
resolve to an older revision.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from adapter.errors import ScriptSourceError
from adapter.scriptstore.ref import ParsedRef


@runtime_checkable
class ScriptStore(Protocol):
    """A source of script text addressed by named ref."""

    #: Short label used in diagnostics, e.g. "dir:/app/script_store".
    name: str

    async def get(self, ref: ParsedRef) -> str | None:
        """Returns script text, or None when this backend has no such ref."""
        ...


class ChainStore:
    """Tries each backend in order; first hit wins.

    Order expresses precedence, so an overlay root listed before the image
    root lets an operator ship a fix without rebuilding, while the image root
    still guarantees a resolvable default.
    """

    def __init__(self, backends: list[ScriptStore]) -> None:
        self.backends = [b for b in backends if b is not None]
        self.name = "chain(" + ", ".join(b.name for b in self.backends) + ")"

    async def get(self, ref: ParsedRef) -> str | None:
        for backend in self.backends:
            text = await backend.get(ref)
            if text is not None:
                return text
        return None

    async def read(self, ref: ParsedRef) -> str:
        """Like get(), but turns a miss into the caller-facing 404."""
        text = await self.get(ref)
        if text is None:
            raise ScriptSourceError(
                f"Script ref not found: {ref.raw!r}", code="script_not_found"
            )
        return text
