"""bytes -> URL. The one conversion that needs infrastructure.

Everything backend-specific lives in ``adapter.storage``; this mixin owns only
the two things the engine decides:

  * **the key convention** -- ``<yyyymmdd>/[<prefix>/]<uuid>.<ext>``. The date leads so
    a bucket lifecycle rule can expire a day at a time and an operator can find an
    upload by when it happened; it is also where the buckets these channels write to
    already put objects, hence no prefix by default. A prefix, when set, groups objects
    *within* the day (a deployment's own namespace) and never moves the date out of
    first position. Neither the model nor the request id appears in the key: a key
    becomes the URL handed to the caller, so whatever is in it is readable by whoever
    holds that URL, and the trace already carries both facts for people allowed them.
    A backend with no directories flattens the whole thing; see
    ``FalStore.put``.
  * **the degradation contract** -- storage is an accelerator, so an absent or
    failing store must not fail the request. The caller gets a data URI and a
    log line instead.

That second point is a contract scripts already depend on, so it is preserved
exactly: ``upload_temp_image`` still returns ``str``, and still returns a data
URI when storage is unavailable. Scripts that need "a URL or nothing" already
refuse the data URI themselves with ``storage_unavailable`` (see the openai and
google image scripts); this layer deliberately does not make that decision for
them.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from adapter.ctxapi.base import NeedsCodec
from adapter.trace_attrs import span_elapsed_ms

logger = logging.getLogger(__name__)


class StorageMixin(NeedsCodec):
    """Uploading bytes and getting a URL back, backend-agnostically."""

    async def upload_temp_image(self, data: bytes, ext: str = "png") -> str:
        """Stores bytes and returns a URL; a data URI when storage is off.

        The name says "temp", and on minio that is true: the URL is a
        presigned GET whose lifetime is ``TEMP_IMAGE_TTL``. On fal it is not
        -- the returned URL is public and does not expire, because fal's
        retention is account-level policy. A caller that has to be able to
        stop an object being readable needs an ACL, not a shorter TTL.

        Bounded by ``storage_upload_timeout``, and a timeout degrades exactly
        like every other backend failure -- the store is an accelerator, so a
        wedged upload returns a data URI instead of failing the request. The
        bound buys the *attribution*: without it the upload runs into the phase
        cap and is reported as ``script_timeout``, and on the fal backend the
        degradation leaves no trace at all.

        The guard around the store stays ``except Exception`` on purpose. A
        wider ``except BaseException`` would swallow the task's cancellation --
        which is how the phase cap arrives -- and a phase that swallows it keeps
        running and then returns normally, so ``script_timeout`` would quietly
        stop working. The inner ``asyncio.timeout`` is what makes a timeout
        visible to this guard: it converts the cancellation into a
        ``TimeoutError``, which *is* an ``Exception``.
        """
        store = self.storage
        mime = f"image/{ext}"

        if store is None:
            logger.warning(
                "[dev] no object storage configured; returning a data URI "
                "instead of a link"
            )
            return self.data_uri(data, mime=mime)

        key = self._temp_key(ext)
        # The upload gets its own span because it is the slowest thing a script
        # does that is not the upstream call: the fal backend measured a 21.97 s
        # outlier against a ~1.6 s median, and the 30 s phase cap is under 1.4x
        # that outlier. Without this span, a request the cap killed reports only
        # the phase it died in.
        try:
            with self.logfire.span(
                "storage_put", store=store.name, bytes=len(data), ext=ext
            ) as span, span_elapsed_ms(span):
                # A second guard, deliberately. The outer one turns any failure
                # into a data URI, so the exception never reaches the span and a
                # degraded upload would read as a clean one -- and for the fal
                # backend that degradation is otherwise invisible to the caller.
                try:
                    async with asyncio.timeout(
                        self.settings.storage_upload_timeout
                    ):
                        stored = await store.put(data, key=key, content_type=mime)
                except Exception as exc:  # noqa: BLE001 - re-raised unchanged
                    span.set_attribute("outcome", "error")
                    span.set_attribute(
                        "error_code",
                        "storage_upload_timeout"
                        if isinstance(exc, TimeoutError)
                        else type(exc).__name__,
                    )
                    raise
                span.set_attribute("outcome", "ok")
        except Exception as exc:  # noqa: BLE001 - see the degradation contract
            # The backends raise a wide zoo between them: S3Error and
            # InvalidResponseError from minio-py (an nginx 404 page is not
            # XML), connection-level errors, and whatever the fal SDK
            # surfaces. Any of them means the object store is unusable, and
            # the documented behaviour is to degrade rather than fail the
            # request.
            logger.error(
                "object storage upload failed via %s (%s); falling back to a "
                "data URI",
                store.name,
                exc,
            )
            return self.data_uri(data, mime=mime)

        if not stored.url.startswith(("http://", "https://")):
            # A backend returning something else would break every caller that
            # hands the value to an upstream as a link, and the failure would
            # surface a layer away from its cause. Same escape hatch as above.
            logger.error(
                "object storage backend %s returned a non-URL (%r); falling "
                "back to a data URI",
                store.name,
                stored.url[:32],
            )
            return self.data_uri(data, mime=mime)

        return stored.url

    def _temp_key(self, ext: str) -> str:
        """``<yyyymmdd>/[<prefix>/]<uuid>.<ext>``.

        The date comes first and sits at the bucket root, because that is how the
        buckets these channels write to are already laid out: a lifecycle rule can
        expire a day at a time, and an upload can be found by when it happened.

        A prefix is a namespace *below* the date, not above it, so setting one never
        moves the date out of first position -- that property is what the rule above
        depends on. Slashes are stripped either way, so "" and "/" mean the same thing
        and neither can produce "<date>//<name>".

        The name is the bare uuid, and the model deliberately does **not** appear in it
        (reversed on 2026-09-12, the same day it was added). A key becomes the URL handed
        to the caller, and on the unsigned backends anyone holding that URL -- or reading
        a log, a referrer, or the bucket itself -- would learn which model produced the
        image. That is information about the deployment, and a key is not the place to
        publish it: the trace already carries ``model`` for people who are allowed it
        (``trace_attrs.summarise_request``).

        No request-id segment either, deliberately. Nothing consumed it: no code parses a
        key back, and the trace carries ``request_id`` from ``request.state`` instead (see
        ``logfire_setup._request_attributes``). What it did do was lengthen the URL handed
        to the caller, so it was a cost with no buyer.

        The date is the process's local date, so ``TZ`` decides where the midnight
        boundary falls -- UTC in a container unless the deployment says otherwise. That
        is deliberate: an operator reading the bucket wants the date their own clock
        showed, and there is no client timezone to derive one from.
        """
        date = time.strftime("%Y%m%d")
        name = f"{uuid.uuid4().hex}.{ext}"
        prefix = self.settings.storage_key_prefix.strip("/")
        return f"{date}/{prefix}/{name}" if prefix else f"{date}/{name}"
