"""``minio_colocated``: one bucket, two addresses.

The backend exists for one deployment shape -- uploads over the internal address, links
always over the domain -- so these tests pin the three properties that make that shape
either work or be quietly wrong:

* the bytes go to the internal address first (and *only* there when it works);
* the **URL never follows the address that served** -- an upload that went internally
  still answers with a domain URL a caller can fetch;
* a dead internal address falls back, and is then parked rather than paid for again.

The fakes are deliberately SDK-shaped but not the SDK: the point is the composition, and
a real minio client would need a server.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import logfire
import pytest
from logfire.testing import SimpleSpanProcessor, TestExporter

from adapter.settings import Settings
from adapter.storage.factory import build_storage, missing_storage_config, storage_configured
from adapter.storage.minio_colocated_store import (
    DOMAIN_LABEL,
    INTERNAL_LABEL,
    MinioColocatedStore,
)
from adapter.storage.minio_public_store import MinioPublicStore

PNG = b"\x89PNG\r\n\x1a\n fake body"

ANON_POLICY = (
    '{"Statement": [{"Effect": "Allow", "Principal": "*", '
    '"Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::b/*"]}]}'
)


class _FakeMinio:
    """Records what an upload asked of it, and can pretend to be unreachable."""

    def __init__(self, *, dead: bool = False, bucket: bool = True, ping_raises: bool = False) -> None:
        self.objects: list[dict] = []
        self.uploads = 0
        self.pings = 0
        self._dead = dead
        self._bucket = bucket
        self._ping_raises = ping_raises

    def put_object(self, bucket, key, stream, length, *, content_type=None, metadata=None):
        self.uploads += 1
        if self._dead:
            raise ConnectionError("simulated unreachable address")
        self.objects.append(
            {"bucket": bucket, "key": key, "data": stream.read(), "content_type": content_type}
        )
        return SimpleNamespace(etag="etag", size=length)

    def bucket_exists(self, bucket):
        self.pings += 1
        if self._ping_raises:
            raise ConnectionError("simulated unreachable address")
        return self._bucket and not self._dead

    def get_bucket_policy(self, bucket):
        return ANON_POLICY


def _settings(**overrides) -> Settings:
    """Production's shape: the domain in ``MINIO_ENDPOINT``, the in-cluster address beside it.

    ``minio_secure=True`` because that is what the deployment sets -- and it is also what
    decides the URL's scheme whenever no ``MINIO_PUBLIC_BASE_URL`` overrides the origin.
    """
    base = dict(
        storage_backend="minio_colocated",
        minio_endpoint="s3ai.cn",
        minio_internal_endpoint="minio:19000",
        minio_secure=True,
        # Pinned empty so these assertions describe the backend, not this machine: with an
        # origin set, `public_url` stops deriving from the endpoint and the shape asserted
        # below would silently be the deployment's instead of the code's.
        minio_public_base_url="",
        minio_bucket="adapter-temp",
        minio_access_key="AK",
        minio_secret_key="SK",
    )
    base.update(overrides)
    return Settings(**base)


def _store(internal: _FakeMinio, domain: _FakeMinio, *, settings=None, clock=None):
    """The composition the builder makes, with fakes in place of clients.

    Both halves read the same settings, exactly as the builder arranges them: the URL comes
    from ``MINIO_ENDPOINT``, which is the *domain*, so nothing has to be swapped or
    re-pointed here.
    """
    if settings is None:
        settings = _settings()
    kwargs = {"clock": clock} if clock is not None else {}
    return MinioColocatedStore(
        MinioPublicStore(internal, settings, name=INTERNAL_LABEL),
        MinioPublicStore(domain, settings, name=DOMAIN_LABEL),
        internal_address=settings.minio_internal_endpoint,
        domain_address=settings.minio_endpoint,
        cooldown=30.0,
        **kwargs,
    )


# -- wiring ----------------------------------------------------------------


def test_colocated_backend_builds_and_names_its_two_addresses():
    store = build_storage(_settings())

    assert isinstance(store, MinioColocatedStore)
    # /health reports this, so it has to match STORAGE_BACKEND.
    assert store.name == "minio_colocated"
    # The halves name the *address*, which is what a log line about a failure must say.
    assert store._primary.name == "minio_public@internal"
    assert store._secondary.name == "minio_public@domain"


def test_colocated_needs_both_addresses_and_says_which_one_is_missing():
    """One address missing is not "storage is off": it is a backend that cannot do its job.

    The in-cluster address is the only thing this backend adds, so a deployment that set
    ``STORAGE_BACKEND=minio_colocated`` without it would otherwise upload over the domain
    forever and look perfectly healthy.
    """
    assert missing_storage_config(_settings()) is None
    assert storage_configured(_settings())

    missing_internal = _settings(minio_internal_endpoint="")
    assert missing_storage_config(missing_internal) == "MINIO_INTERNAL_ENDPOINT"
    assert not storage_configured(missing_internal)
    assert build_storage(missing_internal) is None

    public_missing = _settings(minio_endpoint="")
    assert missing_storage_config(public_missing) == "MINIO_ENDPOINT"
    assert build_storage(public_missing) is None


def test_the_public_address_keeps_minio_secure_and_the_internal_one_does_not():
    """One shared flag cannot describe both: production sets MINIO_SECURE=true.

    Reading the in-cluster address's transport from MINIO_SECURE would make it demand TLS
    on a plaintext in-cluster hop -- and this repo's own ``.env`` is exactly that
    combination.
    """
    store = build_storage(
        _settings(minio_secure=True, minio_internal_endpoint="http://minio:19000")
    )

    assert isinstance(store, MinioColocatedStore)
    assert store._primary._client._base_url.is_https is False
    assert store._secondary._client._base_url.is_https is True


def test_a_scheme_less_internal_address_stays_plaintext():
    store = build_storage(_settings(minio_secure=True, minio_internal_endpoint="minio:19000"))

    assert store._primary._client._base_url.is_https is False


# -- upload path -----------------------------------------------------------


async def test_upload_prefers_the_internal_address():
    internal, domain = _FakeMinio(), _FakeMinio()

    stored = await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert len(internal.objects) == 1
    assert domain.uploads == 0, "the domain must not be touched while the internal works"
    assert internal.objects[0]["data"] == PNG
    assert stored.visibility == "public"
    assert stored.expires_at is None


async def test_the_url_never_follows_the_address_that_served():
    """The caller can only reach the domain, so the fast path must not leak the other."""
    internal, domain = _FakeMinio(), _FakeMinio()

    stored = await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert stored.url == "https://s3ai.cn/adapter-temp/temp/req-1/x.png"
    assert "minio:19000" not in stored.url


def test_the_url_comes_from_the_public_address_not_the_internal_one():
    """A caller can only reach the domain, so the link must never name the other address.

    Asserted on what ``build_storage`` actually assembles, not on a hand-made composition.
    Under the previous naming this needed a second, rewritten settings copy to stay true
    (``public_url`` derives its origin from ``minio_endpoint``, which then held the
    *internal* address) -- a copy that could drift. With the addresses named as they are,
    both halves read the same settings and the URL follows ``MINIO_ENDPOINT``; the failure
    mode is no longer expressible, which is the point of the swap.
    """
    store = build_storage(_settings())
    assert isinstance(store, MinioColocatedStore)

    for half in (store._primary, store._secondary):
        url = half.public_url("temp/req-1/x.png")
        assert url == "https://s3ai.cn/adapter-temp/temp/req-1/x.png"
        assert "minio:19000" not in url


async def test_a_public_base_url_override_still_wins():
    """The operator's stated link shape has no bucket segment: https://s3ai.cn/<key>."""
    internal, domain = _FakeMinio(), _FakeMinio()
    settings = _settings(minio_public_base_url="https://s3ai.cn")

    stored = await _store(internal, domain, settings=settings).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert stored.url == "https://s3ai.cn/temp/req-1/x.png"


async def test_a_dead_internal_address_falls_back_to_the_domain():
    internal, domain = _FakeMinio(dead=True), _FakeMinio()

    stored = await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert internal.uploads == 1
    assert len(domain.objects) == 1, "the domain has to carry the bytes"
    assert stored.url == "https://s3ai.cn/adapter-temp/temp/req-1/x.png"


async def test_a_failed_internal_address_is_parked_not_paid_for_again():
    """The 1s(ish) give-up is a per-cooldown cost, not a per-upload one."""
    clock = _Clock()
    internal, domain = _FakeMinio(dead=True), _FakeMinio()
    store = _store(internal, domain, clock=clock)

    await store.put(PNG, key="a.png", content_type="image/png")
    assert internal.uploads == 1

    await store.put(PNG, key="b.png", content_type="image/png")
    assert internal.uploads == 1, "still parked: the internal address must not be retried"
    assert len(domain.objects) == 2

    # Once the park expires, it is tried again -- a restarted MinIO is picked up
    # without an operator, and a healthy one is not lost for the process's lifetime.
    clock.advance(31.0)
    internal._dead = False
    await store.put(PNG, key="c.png", content_type="image/png")
    assert internal.uploads == 2
    assert len(internal.objects) == 1


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- health ----------------------------------------------------------------


async def test_ping_is_true_when_either_address_can_serve():
    assert await _store(_FakeMinio(), _FakeMinio()).ping() is True

    # Internal unreachable, domain fine: uploads still work, so the deployment is up.
    assert await _store(_FakeMinio(dead=True), _FakeMinio()).ping() is True


async def test_ping_is_false_when_the_bucket_is_not_readable():
    """A public-URL backend over a bucket that forbids anonymous reads uploads fine and
    403s at every caller, so the policy is part of the answer rather than a detail."""
    internal, domain = _FakeMinio(bucket=False), _FakeMinio(bucket=False)

    assert await _store(internal, domain).ping() is False


async def test_upload_never_signs():
    """The composition is two ``minio_public`` halves, so no signature is ever produced.

    ``MinioStore`` signs, ``MinioPublicStore`` does not -- and signing here would also
    mean the URL's host is whatever signed it, which is the bug this backend exists to
    avoid. The fake records the call rather than relying on it being absent.
    """
    import io

    internal, domain = _SignRecordingMinio(), _SignRecordingMinio()

    stored = await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert internal.presigns == [] and domain.presigns == []
    assert "X-Amz-Signature" not in stored.url
    assert stored.url == "https://s3ai.cn/adapter-temp/temp/req-1/x.png"
    assert io.BytesIO(PNG).read() == PNG


class _SignRecordingMinio(_FakeMinio):
    """``presigned_get_object`` is the one call this backend must never make."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.presigns: list[tuple] = []

    def presigned_get_object(self, bucket, key, expires=None):
        self.presigns.append((bucket, key, expires))
        return f"https://signed.example/{bucket}/{key}"


# -- reporting -------------------------------------------------------------


@pytest.fixture
def spans():
    """A Logfire exporter this file can read, in the repo's own harness style."""
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    return exporter


def _fallback_events(exporter: TestExporter) -> list[dict]:
    logfire.force_flush()
    return [
        span["attributes"]
        for span in exporter.exported_spans_as_dict()
        if span["name"] == "storage_fallback"
    ]


async def test_a_dead_internal_address_is_reported(spans):
    """The upload succeeds, so nothing else in the trace would say the address is dark.

    This is the whole point: ``storage_put`` reports ``outcome=ok`` (the domain served),
    and a ``logger.warning`` never reaches Logfire -- the app configures plain logging
    and attaches no handler. Without this event, a pod that has quietly moved to the
    domain for every upload looks identical to a healthy one.
    """
    internal, domain = _FakeMinio(dead=True), _FakeMinio()

    await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    events = _fallback_events(spans)
    assert len(events) == 1, events
    assert events[0]["address"] == "minio:19000"
    assert events[0]["serving"] == "s3ai.cn"
    assert events[0]["check"] == "upload"
    assert events[0]["error_code"] == "ConnectionError"
    assert "simulated unreachable address" in events[0]["error_message"]


async def test_a_healthy_internal_address_reports_nothing(spans):
    """The event has to mean something: it fires on the fallback, not on every upload."""
    internal, domain = _FakeMinio(), _FakeMinio()

    await _store(internal, domain).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert _fallback_events(spans) == []


async def test_the_report_is_the_transition_not_every_upload(spans):
    """While the address is parked there is nothing new to say."""
    clock = _Clock()
    internal, domain = _FakeMinio(dead=True), _FakeMinio()
    store = _store(internal, domain, clock=clock)

    await store.put(PNG, key="a.png", content_type="image/png")
    await store.put(PNG, key="b.png", content_type="image/png")
    assert len(_fallback_events(spans)) == 1

    clock.advance(31.0)
    internal._dead = False
    await store.put(PNG, key="c.png", content_type="image/png")
    assert len(_fallback_events(spans)) == 1, "a recovery is not a fallback event"


async def test_the_health_probe_path_reports_too(spans):
    """It has no request span of its own, so this event is its only trace."""
    internal, domain = _FakeMinio(ping_raises=True), _FakeMinio()

    assert await _store(internal, domain).ping() is True

    events = _fallback_events(spans)
    assert len(events) == 1, events
    assert events[0]["check"] == "health_probe"
    assert events[0]["error_code"] == "ConnectionError"


async def test_a_probe_that_reports_unusable_has_no_exception_to_quote(spans):
    """``ping`` returning False is a finding with no traceback; the event still fires."""
    internal, domain = _FakeMinio(bucket=False), _FakeMinio(bucket=False)

    assert await _store(internal, domain).ping() is False

    events = _fallback_events(spans)
    assert len(events) == 1, events
    assert events[0]["check"] == "health_probe"
    assert "error_code" not in events[0]
