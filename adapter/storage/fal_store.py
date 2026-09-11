"""fal.ai's CDN.

Not S3, and not presigned. An upload returns a **public, long-lived** URL, so
three things have to be said out loud rather than inferred from the URL:

  * ``visibility`` is ``"public"``, and that is the point of this backend rather
    than a shortcoming of it: a client, or an upstream model, can fetch the URL
    with no credential and no expiry -- what a presigned URL cannot offer.
    Measured, not assumed: ``initial_acl`` was probed with both ``forbid`` and
    ``hide`` and the object stayed readable anonymously (2026-09-11, live key).
    The SDK does put ``X-Fal-Object-Lifecycle(-Preference): {"initial_acl":
    {...}}`` on the v3 upload; the upstream accepts and ignores it. So the ACL
    surface is unavailable, and the public behaviour is guaranteed rather than
    incidental -- a property to rely on, not a default that might change.
  * ``expires_at`` is None, because retention is fal's account-level policy --
    the longest window on offer. The decision here is to inherit it (send no
    ``expires_in``) rather than override per request, so this backend
    deliberately does not manage a TTL.
  * Permanence is also why withdrawal is not on the table: fal exposes no
    delete on this API, and an ACL cannot hide the object. ``ctx`` keeps
    ``upload_temp_image``'s name and its ``str`` return for script
    compatibility, but on fal "temporary" describes nothing -- the link keeps
    working long after the request that made it, which is what the callers want.
    The one thing to keep in mind is that it applies to *whatever* the engine
    uploads, so a channel using fal for re-hosted **input** images publishes
    those too.

The SDK rather than a hand-rolled two-step HTTP upload, for two reasons that
are not about line count: it resolves credentials per instance -- so a channel
cannot inherit another deployment's key, which is what makes the SDK usable in
a multi-tenant adapter at all -- and its primary path is the CDN-token flow.
The ``/storage/upload/initiate`` REST endpoint the public docs lead you to is
only the SDK's *fallback* repository, so hand-rolling it would mean shipping the
path fal takes when its main CDN is already failing.

That fallback is worth knowing about: it answers ``Invalid storage type`` on the
account this was measured against, so when the v3 path fails the upload fails
outright rather than being rescued.
"""

from __future__ import annotations

from adapter.storage.base import StoredObject

#: fal's REST control plane. The SDK posts here for a short-lived CDN token
#: before each upload burst; the probe reuses it because it is the cheapest
#: authenticated call and exercises the same credential and host as ``put``.
_TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"


class FalStore:
    """Object store backed by the fal-client SDK's CDN upload."""

    name = "fal"

    def __init__(self, client, settings, http=None) -> None:
        self._client = client
        self._settings = settings
        #: The process-wide aiohttp session, used only by ``ping``. Optional
        #: so a standalone construction (unit tests, direct ContextCore use)
        #: still works without dragging in the application lifespan.
        self._http = http

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        # fal has no directories: only the basename survives as the CDN object
        # name. Flattening the engine's key here, rather than asking callers
        # to guess which backends have a path hierarchy, is exactly the
        # mapping the port is for.
        file_name = key.rsplit("/", 1)[-1]
        url = await self._client.upload(data, content_type, file_name=file_name)
        return StoredObject(url=url, key=key, visibility="public", expires_at=None)

    async def ping(self) -> bool:
        """Asks for a CDN token: the first call ``put`` would make anyway.

        Deliberately not an upload. A probe that writes an object would leave
        a permanent public artifact on every health check, since fal exposes
        no delete on this API surface.

        The empty JSON body is required, not decoration. A POST without one is
        answered 422 ``{"detail":[{"loc":["body"],"msg":"Field required"}]}``,
        which made every health check report this backend as degraded while
        uploads worked fine. ``fal_client`` sends ``json={}`` for the same
        reason (``CDNTokenManager._refresh_token``), headers included.
        """
        if not self._settings.fal_key:
            return False

        headers = {
            "Authorization": f"Key {self._settings.fal_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._http is not None:
            async with self._http.post(_TOKEN_URL, headers=headers, json={}) as resp:
                return resp.status < 400

        # No shared session injected (standalone use). A short-lived one is
        # cheaper than reaching for a global that may not exist.
        import aiohttp

        async with aiohttp.ClientSession() as session, session.post(
            _TOKEN_URL, headers=headers, json={}
        ) as resp:
            return resp.status < 400


def build_client(api_key: str):
    """Constructs the SDK client, or raises with an actionable message.

    The import lives here rather than at module scope so the dependency stays
    optional: a deployment running the minio backend never pays for httpx,
    msgpack and websockets, which fal-client pulls in for the queue,
    streaming and realtime model-calling paths this adapter never touches.
    """
    try:
        from fal_client import AsyncClient
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ImportError(
            "STORAGE_BACKEND=fal requires the fal-client SDK. Install it with "
            "`pip install 'image-adapter[fal]'` or `pip install fal-client`."
        ) from exc

    # The key is injected rather than left to fal-client's own environment
    # lookup, so the credential this process uploads with is the one the
    # deployment configured and not a stray FAL_KEY it inherited.
    return AsyncClient(key=api_key)
