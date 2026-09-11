"""The same bucket, read anonymously.

A presigned URL cannot outlive seven days -- that is the SigV4 ceiling, and
minio-py refuses to sign past it ("expires must be between 1 second to 7
days"). A deployment that must hand a caller a link which keeps working for
longer has three ways out: swap the backend (fal), re-sign behind a redirect
we serve, or make the object readable with no signature at all. This is the
third one. The bucket policy grants anonymous ``s3:GetObject``, so the URL
carries no credentials and there is no expiry to reach.

What that costs is reported where it belongs, in ``visibility`` and
``expires_at``: the URL is public and does not expire, so anything uploaded
through this backend is **published**, and cannot be unpublished through this
API -- fal exposes no delete and neither does a bucket policy that still says
allow. Objects that must stay private belong on the presigned backend.

The upload is the same S3 call. Only the URL and the description differ, which
is why this subclasses ``MinioStore`` instead of repeating it.

Required on the storage side (nothing this process can arrange):

  * the bucket policy must allow ``s3:GetObject`` to ``*``. ``ping`` checks
    that, so a deployment which forgot reports degraded instead of handing out
    URLs that 403.
  * object keys must not be guessable. Ours are ``temp/<uuid4>/<uuid4>``, so
    "public" means "anyone holding the link" rather than "anyone who asks" --
    but that is a property of the key convention, not of the policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import quote

from adapter.storage.base import StoredObject
from adapter.storage.minio_store import MinioStore

logger = logging.getLogger(__name__)


def _allows_anonymous_read(policy: str) -> bool:
    """Whether a bucket policy lets an unauthenticated GET through.

    Deliberately narrow: it looks for a statement that *allows* ``*`` to
    ``s3:GetObject`` and nothing subtler. A policy that grants it conditionally
    (by source IP, say) still counts as allowing it here -- we cannot evaluate
    conditions, and reporting healthy for a policy that might work is the same
    call ``MinioStore.ping`` makes about a bucket that might disappear.
    """
    try:
        document = json.loads(policy)
    except (TypeError, ValueError):
        return False
    if not isinstance(document, dict):
        return False

    statements = document.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return False

    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        if not _is_everyone(statement.get("Principal")):
            continue
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else list(actions or [])
        if any(a in ("s3:GetObject", "s3:*", "*") for a in actions):
            return True
    return False


def _is_everyone(principal) -> bool:
    """``"*"`` or ``{"AWS": "*"}`` or ``{"AWS": ["*"]}`` -- the three spellings."""
    if principal == "*":
        return True
    if isinstance(principal, dict):
        aws = principal.get("AWS")
        if aws == "*":
            return True
        if isinstance(aws, list) and "*" in aws:
            return True
    return False


class MinioPublicStore(MinioStore):
    """The minio bucket, addressed without a signature."""

    name = "minio_public"

    def public_url(self, key: str) -> str:
        """The unsigned URL a client would fetch. Pure; no I/O, no signing.

        Path style by default (``scheme://endpoint/bucket/key``), which is what
        a bare MinIO answers. ``MINIO_PUBLIC_BASE_URL`` overrides the origin
        for a bucket fronted by a CDN, a custom domain, or a gateway that only
        routes a sub-path -- the derived form would then name a host the client
        cannot reach.
        """
        base = self._settings.minio_public_base_url.strip()
        if not base:
            scheme = "https" if self._settings.minio_secure else "http"
            bucket = self._settings.minio_bucket
            base = f"{scheme}://{self._settings.minio_endpoint}/{bucket}"
        # safe="/" keeps the temp/<request-id>/<uuid>.<ext> shape readable in
        # logs while still escaping anything a key should never contain.
        return f"{base.rstrip('/')}/{quote(key, safe='/')}"

    def _put_and_url(self, data: bytes, key: str, content_type: str) -> str:
        self._upload(data, key, content_type)
        return self.public_url(key)

    def _describe(self, key: str, url: str) -> StoredObject:
        """No signature, so no expiry to report -- and no withdrawal either."""
        return StoredObject(url=url, key=key, visibility="public", expires_at=None)

    async def ping(self) -> bool:
        """``bucket_exists``, plus proof that anonymous reads are allowed.

        A public-URL backend over a private bucket is the worst failure this
        layer can have: uploads succeed, every link 403s at the caller, and
        nothing on the write path notices. The read path cannot be exercised
        without writing an object, which a health check must never do, so the
        policy is read instead -- it is the thing that makes the URL work, and
        reading it is one authenticated call.

        Skipped when ``MINIO_PUBLIC_BASE_URL`` is set: URLs then do not address
        the bucket at all, so its policy says nothing about whether they
        resolve, and asserting on it would call a working deployment degraded.
        """
        if not await super().ping():
            return False
        if self._settings.minio_public_base_url.strip():
            return True

        try:
            policy = await asyncio.to_thread(
                self._client.get_bucket_policy, self._settings.minio_bucket
            )
        except Exception as exc:  # noqa: BLE001 - NoSuchBucketPolicy is the usual
            logger.warning(
                "bucket %s has no readable policy (%s); anonymous URLs from "
                "STORAGE_BACKEND=minio_public will not be fetchable",
                self._settings.minio_bucket,
                exc,
            )
            return False
        return _allows_anonymous_read(policy)
