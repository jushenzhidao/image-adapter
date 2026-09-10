"""Script-facing ctx API, split into composable mixin modules.

`AdapterContext` used to carry every script helper as a method on one class,
so each new helper edited the same file that owns request lifecycle and infra
handles. The helpers change far more often than the lifecycle does, so they
now live in one module per concern and the context is assembled from them:

    class AdapterContext(CodecMixin, ImageRefMixin, StorageMixin,
                         BudgetMixin, PlanMixin, ContextCore)

Composition rules that keep this extensible without surprises:

  * A mixin never defines ``__init__``. Per-request state is created by
    ``ContextCore`` and mixins declare what they need as annotations only,
    so MRO order can change freely.
  * A mixin reaches infra only through attributes ``ContextCore`` provides
    (``settings``, ``http``, ``cache``, ``storage``): no module imports
    aiohttp/redis/minio itself.
  * Cross-mixin calls go through ``self``, never by importing a sibling. The
    dependency is declared by inheriting a ``Needs*`` marker from ``base``,
    which is what keeps those calls type-checked across the split.
  * Adding a helper group = new module + one name in the bases tuple.

Method names on the composed context are flat and unchanged, so scripts keep
calling ``ctx.image_b64(...)`` with no notion that mixins exist.
"""

from __future__ import annotations

from adapter.ctxapi.base import CtxMixin, NeedsCodec, NeedsStorage
from adapter.ctxapi.budget import BudgetMixin
from adapter.ctxapi.codec import CodecMixin
from adapter.ctxapi.image_ref import ImageRefMixin
from adapter.ctxapi.plan import PlanMixin, RequestPlan
from adapter.ctxapi.storage import StorageMixin

#: Every mixin composed into ``AdapterContext``, in MRO order. Registering a
#: new group here is the whole wiring step.
CTX_MIXINS = (
    CodecMixin,
    ImageRefMixin,
    StorageMixin,
    BudgetMixin,
    PlanMixin,
)

__all__ = [
    "CTX_MIXINS",
    "BudgetMixin",
    "CodecMixin",
    "CtxMixin",
    "ImageRefMixin",
    "NeedsCodec",
    "NeedsStorage",
    "PlanMixin",
    "RequestPlan",
    "StorageMixin",
]
