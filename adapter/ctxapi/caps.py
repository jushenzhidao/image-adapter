"""Script-facing access to a vendor's measured capability table.

Facts live in ``capabilities/<vendor>.json`` (see ``adapter/capabilities.py``) and
reach a script only through this mixin. That indirection is deliberate: the sandbox
forbids importing adapter modules, and keeping one narrow read path is what stops
scripts from growing a second copy of data that a deployment is supposed to be able
to hot-fix.

``None`` is a real answer -- "no facts for this model". A script must handle it by
sending *less* (let the upstream use its defaults) rather than by carrying its own
fallback table, because a second copy is precisely the drift this exists to prevent.
"""

from __future__ import annotations

from typing import Any

from adapter.capabilities import lookup
from adapter.ctxapi.base import CtxMixin


class CapsMixin(CtxMixin):
    """Read-only view of a vendor's capability facts."""

    def caps(self, vendor: str, model: str | None = None) -> dict[str, Any] | None:
        """Facts for one model, alias- and suffix-resolved, or None if unknown.

        The returned dict carries the resolved ``model`` id alongside the facts, so
        a caller that must write the model into a URL gets the normalised name
        rather than whichever spelling the client happened to send.
        """
        return lookup(vendor, model, self.settings.capability_roots)
