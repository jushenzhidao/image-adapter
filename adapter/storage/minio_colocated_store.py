"""One bucket, two addresses: upload over the internal address, fall back to the domain.

This is the deployment shape the 2026-09-12 review settled on, in the operator's words:
「同机器 pod 共享下内网上传，跨机器就用域名」, and «内网地址与域名之间的映射».

What makes it a backend of its own, rather than a ``FallbackStore`` of two existing
ones, is that the two halves are **not interchangeable**: they are two addresses of the
same bucket and must hand back the **same** URL. Read the composition literally --
``FallbackStore`` chooses *which backend* served, and reports that in
``StoredObject.visibility``; here the answer is the same either way, and the only
difference is which transport carried the bytes.

Consequences worth stating, because they are the reason for each choice below:

* **The URL never follows the address that served.** It is the domain, always
  (``MinioPublicStore.public_url``), because the domain is the only address the *caller*
  can reach. An upload that went over the internal address still returns a domain URL.
* **Both halves are ``minio_public``**, i.e. the link carries no signature and the bucket
  policy must allow anonymous ``s3:GetObject``. That is what makes "always the domain" a
  valid answer for an upload that may have happened elsewhere. A deployment whose bucket
  is *private* needs the presigning variant instead: upload over the internal address,
  but sign with the **domain** client so the URL's host is the domain. That is a
  ``_put_and_url`` override away and is deliberately not implemented here, because it is
  a different promise (the link expires).
* **The internal attempt fails fast.** The pool installed for it sets
  ``Retry(total=0)`` and a 1s connect timeout. Measured on 2026-09-12 against a closed
  port: the ordinary builder's ``Retry(total=5, backoff_factor=0.2)`` takes **6.02s** to
  give up, the fast pool **0.00s**. Since a cross-machine pod pays this on every cold
  start, and the whole point is to fall back, the ordinary pool would defeat the design.
  The domain half keeps the ordinary pool: it is the last resort, and a transient 5xx
  there is worth retrying.
* **A parked internal address comes back on its own**, via the parent's cooldown and the
  health probe -- see ``FallbackStore`` for the measured reasoning. This is why the
  decision is not "sticky forever": a MinIO that restarts must not cost the internal path
  for the rest of the process's life.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from adapter.storage.fallback_store import FallbackStore
from adapter.trace_attrs import record_storage_fallback

#: Reported by /health and in log fields.
NAME = "minio_colocated"

#: Role labels for the two halves, so a log line says *which address* failed rather than
#: naming the same backend twice.
INTERNAL_LABEL = "minio_public@internal"
DOMAIN_LABEL = "minio_public@domain"


class MinioColocatedStore(FallbackStore):
    """Uploads try the internal address, then the domain; links are always the domain.

    Thin on purpose: the try-park-recover machinery, the cooldown, the health-probe
    behaviour and the "which one served" logging all live in ``FallbackStore`` and are
    already measured and tested there. What this class adds is a name that matches
    ``STORAGE_BACKEND=minio_colocated`` and somewhere for the reasoning above to live.
    """

    def __init__(
        self,
        internal,
        domain,
        *,
        internal_address: str,
        domain_address: str,
        cooldown: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            internal,
            domain,
            cooldown=cooldown,
            clock=clock,
            # The parent derives "a+b" from its halves, which for two minio_public
            # instances would read "minio_public+minio_public" -- true, and useless.
            name=NAME,
        )
        #: Carried for the report, not for the transport: the requests go to the clients
        #: the halves hold, while a person reading the trace needs the addresses.
        self._internal_address = internal_address
        self._domain_address = domain_address

    def _on_primary_failure(self, exc: BaseException | None, *, probe: bool) -> None:
        """Reports the fallback to Logfire, naming both addresses.

        This is the only place that says *which* address went dark, and it is the reason
        the hook exists: the upload itself succeeds over the domain, so the request's own
        span reads clean, and a ``logger.warning`` does not reach Logfire (this app
        configures plain ``logging``, not a Logfire handler).
        """
        record_storage_fallback(
            address=self._internal_address,
            serving=self._domain_address,
            error=exc,
            probe=probe,
        )
