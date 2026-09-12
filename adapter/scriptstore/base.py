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

A concrete version that *no* root carries degrades to ``@stable`` before the
chain gives up (see ``ChainStore.get``). That is the state a channel header is
left in when a version is retired, so it is treated as an expected input rather
than a caller error — which is also why every manifest entry is expected to
define ``stable``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from adapter.errors import ScriptSourceError
from adapter.scriptstore.manifest import STABLE_ALIAS
from adapter.scriptstore.ref import ParsedRef

logger = logging.getLogger(__name__)


@runtime_checkable
class ScriptStore(Protocol):
    """A source of script text addressed by named ref."""

    #: Short label used in diagnostics, e.g. "dir:/app/script_store".
    name: str

    async def get(self, ref: ParsedRef) -> str | None:
        """Returns script text, or None when this backend has no such ref."""
        ...


@dataclass(frozen=True)
class ServedScript:
    """Script text and the ref it was actually found under.

    ``served`` differs from ``requested`` exactly when the chain degraded a
    retired version to ``@stable``. Callers that report provenance need both:
    the text alone cannot tell them apart, and the two refs may resolve to the
    same file (one version) or not (an overlay hot-fix).
    """

    text: str
    requested: str
    served: str

    @property
    def degraded(self) -> bool:
        return self.served != self.requested


class ChainStore:
    """Tries each backend in order; first hit wins, then ``@stable``.

    Order expresses precedence, so an overlay root listed before the image
    root lets an operator ship a fix without rebuilding, while the image root
    still guarantees a resolvable default.

    A version that exists *is* served as asked -- the fallback never substitutes
    for a hit, so a pinned version still wins wherever a root provides it.
    """

    def __init__(self, backends: Sequence[ScriptStore]) -> None:
        # A Sequence, not a list: the chain only reads the caller's collection,
        # and the narrower type rejected `list[DirStore]` for the purely
        # technical reason that lists are invariant.
        self.backends = [b for b in backends if b is not None]
        self.name = "chain(" + ", ".join(b.name for b in self.backends) + ")"

    async def get(self, ref: ParsedRef) -> str | None:
        """Script text for ``ref``, or None when nothing here serves it."""
        served = await self._serve(ref)
        return served.text if served is not None else None

    async def _serve(self, ref: ParsedRef) -> ServedScript | None:
        """``ref``'s text plus the ref it was found under, or None on a miss."""
        text = await self._first_hit(ref)
        if text is not None:
            return ServedScript(text=text, requested=ref.raw, served=ref.raw)
        fallback = _stable_ref(ref)
        if fallback is None:
            return None
        logger.warning(
            "%s: %r is not served here; falling back to %r",
            self.name,
            ref.raw,
            fallback.raw,
        )
        text = await self._first_hit(fallback)
        if text is None:
            return None
        return ServedScript(text=text, requested=ref.raw, served=fallback.raw)

    async def _first_hit(self, ref: ParsedRef) -> str | None:
        """Walks the backends in precedence order; first hit wins."""
        for backend in self.backends:
            text = await backend.get(ref)
            if text is not None:
                return text
        return None

    async def load(self, ref: ParsedRef) -> ServedScript:
        """Like read(), but also says which ref the text was found under.

        The provenance is what a trace needs: a degraded read and a pinned one
        both return text, and only this tells them apart.
        """
        served = await self._serve(ref)
        if served is None:
            fallback = _stable_ref(ref)
            tried = f" (or its {fallback.raw!r} fallback)" if fallback else ""
            raise ScriptSourceError(
                f"Script ref not found: {ref.raw!r}{tried}",
                code="script_not_found",
            )
        return served

    async def read(self, ref: ParsedRef) -> str:
        """Like get(), but turns a miss into the caller-facing 404."""
        return (await self.load(ref)).text


def _stable_ref(ref: ParsedRef) -> ParsedRef | None:
    """The ``@stable`` ref to degrade to, or None when there is nothing to try.

    Only a ref naming a concrete version degrades. Two deliberate non-cases: a
    versionless ref has no version to replace (and "no version" is a malformed
    header, not a retired one), while ``@stable`` itself would only repeat the
    lookup that just missed.
    """
    if not ref.version or ref.version == STABLE_ALIAS:
        return None
    body = ref.raw.partition("@")[0]
    return ParsedRef(raw=f"{body}@{STABLE_ALIAS}", parts=ref.parts, version=STABLE_ALIAS)
