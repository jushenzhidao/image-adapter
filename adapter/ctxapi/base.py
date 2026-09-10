"""Shared plumbing for ctx mixins.

A mixin is not independently instantiable: it assumes the attributes that
``ContextCore`` sets up. ``CtxMixin`` states that contract for the type
checker without adding any runtime behaviour, which is what lets mixins stay
free of ``__init__`` and therefore free of MRO ordering hazards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import aiohttp

    from adapter.settings import Settings


class CtxMixin:
    """Attribute contract every mixin may rely on.

    Declared as annotations only. Assigning defaults here would shadow the
    real per-request values that ``ContextCore.__init__`` writes.
    """

    if TYPE_CHECKING:
        request_id: str
        endpoint: str
        settings: Settings
        options: dict[str, Any]

        @property
        def http(self) -> aiohttp.ClientSession: ...

        @property
        def cache(self) -> Any: ...

        @property
        def storage(self) -> Any | None: ...


class NeedsCodec(CtxMixin):
    """Declares the codec helpers a mixin consumes from its siblings.

    Cross-mixin calls are the one place a mixin split can rot silently: the
    body of one mixin calls ``self.<method>`` that another mixin owns, and
    nothing checks the two agree. Inheriting this marker states the
    dependency, so a new mixin author can see the available contract and the
    type checker verifies the call sites.

    The declarations are ``TYPE_CHECKING``-only, so at runtime this is an
    empty class that cannot shadow the real implementation. Because the
    composed context inherits both this and ``CodecMixin``, a signature that
    drifts apart is reported as an incompatible-override error rather than
    going unnoticed.
    """

    if TYPE_CHECKING:

        def encode_b64(self, data: bytes) -> str: ...

        def decode_b64(self, value: str) -> bytes: ...

        def data_uri(self, data: bytes, mime: str = ...) -> str: ...

        def sniff_mime(self, data: bytes) -> str: ...

        @staticmethod
        def is_url(value: str) -> bool: ...

        @staticmethod
        def is_data_uri(value: str) -> bool: ...


class NeedsStorage(CtxMixin):
    """Declares the storage helper that shape conversion depends on.

    Same contract-and-check rationale as ``NeedsCodec``; kept separate so a
    mixin only declares the siblings it actually calls.
    """

    if TYPE_CHECKING:

        async def upload_temp_image(self, data: bytes, ext: str = ...) -> str: ...


class SupportsCtx(Protocol):
    """Structural view of the composed context, for helpers taking a ctx."""

    request_id: str
    settings: Any
