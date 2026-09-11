"""Object-storage port: dispatch, the two backends, and the degradation contract.

These are unit tests with fake SDK clients, so they pin the parts this repo
decides -- which key is built, what the result claims about the URL, which SDK
arguments are sent -- rather than the parts the SDKs decide. The integration
suite covers the same path end to end through ``FakeStorage``, and
``tools/probe_object_storage.py`` covers whether the real vendors agree.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from pydantic import ValidationError

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.settings import Settings
from adapter.storage import (
    FallbackStore,
    FalStore,
    MinioPublicStore,
    MinioStore,
    StoredObject,
    build_storage,
    missing_storage_config,
    storage_configured,
)
from adapter.storage import factory as storage_factory

PNG = b"\x89PNG\r\n\x1a\n"


# --- settings guard --------------------------------------------------------


def test_an_unknown_backend_fails_at_startup_instead_of_falling_back():
    """`extra="ignore"` would make a typo'd STORAGE_BACKND silently keep the
    default. A Literal turns it into a startup error, which is the only place
    it can still be seen."""
    with pytest.raises(ValidationError):
        Settings(storage_backend="minoi")


@pytest.mark.parametrize(
    ("backend", "overrides", "expected"),
    [
        ("minio", {"minio_endpoint": ""}, "MINIO_ENDPOINT"),
        ("minio", {"minio_endpoint": "s3.example"}, None),
        ("minio_public", {"minio_endpoint": ""}, "MINIO_ENDPOINT"),
        ("minio_public", {"minio_endpoint": "s3.example"}, None),
        ("fal", {"fal_key": ""}, "FAL_KEY"),
        ("fal", {"fal_key": "k"}, None),
    ],
)
def test_the_missing_key_is_named_per_backend(backend, overrides, expected):
    """Named, not a bare False: "off" and "misconfigured" behave identically at
    the call site and only one of them is a deployment mistake."""
    settings = Settings(storage_backend=backend, **overrides)
    assert missing_storage_config(settings) == expected
    assert storage_configured(settings) is (expected is None)


def test_a_key_for_the_other_backend_does_not_count():
    """A fal key must not make a minio deployment look configured."""
    settings = Settings(storage_backend="minio", minio_endpoint="", fal_key="k")
    assert not storage_configured(settings)


# --- factory dispatch ------------------------------------------------------


def test_minio_backend_builds_a_minio_store():
    store = build_storage(Settings(storage_backend="minio", minio_endpoint="s3.example"))
    assert isinstance(store, MinioStore)
    assert store.name == "minio"


def test_fal_backend_builds_a_fal_store(monkeypatch):
    """The SDK client is stubbed rather than imported, so this asserts the
    *dispatch* without making fal-client a test-time requirement of the whole
    suite."""
    sentinel = object()
    monkeypatch.setattr(storage_factory, "build_client", lambda key: sentinel)

    store = build_storage(Settings(storage_backend="fal", fal_key="live-key"))

    assert isinstance(store, FalStore)
    assert store.name == "fal"
    assert store._client is sentinel


def test_the_shared_session_reaches_the_backend_that_needs_it(monkeypatch):
    """minio carries its own urllib3 pool and ignores it; fal probes with it."""
    monkeypatch.setattr(storage_factory, "build_client", lambda key: object())
    session = object()

    fal = build_storage(Settings(storage_backend="fal", fal_key="k"), http=session)
    assert fal._http is session

    # Accepted and dropped, so every builder has one signature.
    minio = build_storage(
        Settings(storage_backend="minio", minio_endpoint="s3.example"), http=session
    )
    assert isinstance(minio, MinioStore)


# --- minio backend ---------------------------------------------------------


class _FakeMinio:
    """The slice of minio-py this backend uses. Records what it was asked."""

    def __init__(self, bucket_exists: bool = True, policy: str | None = None) -> None:
        self.objects: list[dict] = []
        self.presigns: list[tuple] = []
        self.policy_reads: list[str] = []
        self._bucket_exists = bucket_exists
        self._policy = policy

    def put_object(self, bucket, key, data, length, content_type=None):
        self.objects.append(
            {
                "bucket": bucket,
                "key": key,
                "data": data.read(),
                "length": length,
                "content_type": content_type,
            }
        )

    def presigned_get_object(self, bucket, key, expires=None):
        self.presigns.append((bucket, key, expires))
        return f"https://s3.test/{bucket}/{key}?X-Amz-Signature=abc"

    def bucket_exists(self, bucket):
        return self._bucket_exists

    def get_bucket_policy(self, bucket):
        self.policy_reads.append(bucket)
        if self._policy is None:
            # What minio-py raises when the bucket has no policy at all.
            raise RuntimeError("NoSuchBucketPolicy")
        return self._policy


def _minio_store(client: _FakeMinio, **overrides) -> MinioStore:
    overrides.setdefault("minio_endpoint", "s3.example")
    overrides.setdefault("minio_bucket", "adapter-temp")
    settings = Settings(storage_backend="minio", **overrides)
    return MinioStore(client, settings)


async def test_minio_put_reports_a_presigned_url_that_expires():
    client = _FakeMinio()
    before = time.time()

    stored = await _minio_store(client, temp_image_ttl=1200).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert stored.url == "https://s3.test/adapter-temp/temp/req-1/x.png?X-Amz-Signature=abc"
    assert stored.key == "temp/req-1/x.png"
    assert stored.visibility == "presigned"
    # The presign expiry is the only TTL this backend enforces, so expires_at
    # has to agree with the value handed to the SDK -- not merely exist.
    assert 1200 <= stored.expires_at - before < 1205


async def test_minio_puts_the_content_type_and_the_exact_byte_count():
    client = _FakeMinio()

    await _minio_store(client).put(PNG, key="temp/req-1/x.png", content_type="image/png")

    obj = client.objects[0]
    assert obj["data"] == PNG
    assert obj["length"] == len(PNG)
    # Stored, not inferred: an object with no type is served as a download.
    assert obj["content_type"] == "image/png"


async def test_minio_presigns_with_the_configured_ttl():
    client = _FakeMinio()

    await _minio_store(client, temp_image_ttl=900).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    bucket, key, expires = client.presigns[0]
    assert (bucket, key) == ("adapter-temp", "temp/req-1/x.png")
    assert expires == timedelta(seconds=900)


async def test_minio_ping_is_degraded_when_the_bucket_is_gone():
    """A reachable endpoint with no bucket fails every upload, so reporting the
    round-trip alone would call a broken deployment healthy."""
    assert await _minio_store(_FakeMinio(bucket_exists=False)).ping() is False
    assert await _minio_store(_FakeMinio(bucket_exists=True)).ping() is True


# --- fal backend -----------------------------------------------------------


class _FakeFal:
    """The slice of fal-client's AsyncClient this backend uses."""

    def __init__(self, url: str = "https://v3b.fal.media/files/b/abc/x.png", error=None):
        self.calls: list[dict] = []
        self._url = url
        self._error = error

    async def upload(self, data, content_type, **kwargs):
        self.calls.append({"data": data, "content_type": content_type, **kwargs})
        if self._error is not None:
            raise self._error
        return self._url


def _fal_store(client: _FakeFal, **overrides) -> FalStore:
    overrides.setdefault("fal_key", "live-key")
    settings = Settings(storage_backend="fal", **overrides)
    return FalStore(client, settings)


async def test_fal_put_reports_a_public_url_with_no_expiry():
    stored = await _fal_store(_FakeFal()).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert stored.url == "https://v3b.fal.media/files/b/abc/x.png"
    assert stored.visibility == "public"
    # Retention is fal's account policy and this backend deliberately does not
    # override it, so it must not pretend to know a deadline.
    assert stored.expires_at is None


async def test_fal_flattens_the_key_because_the_cdn_has_no_directories():
    client = _FakeFal()

    stored = await _fal_store(client).put(
        PNG, key="temp/req-1/abc123.png", content_type="image/png"
    )

    assert client.calls[0]["file_name"] == "abc123.png"
    assert client.calls[0]["content_type"] == "image/png"
    assert client.calls[0]["data"] == PNG
    # The engine still gets its own key back, so it can correlate the upload.
    assert stored.key == "temp/req-1/abc123.png"


async def test_fal_sends_no_lifecycle_override():
    """The decision is to inherit fal's account-level retention. Passing
    expires_in would silently shorten every uploaded object's life."""
    client = _FakeFal()

    await _fal_store(client).put(PNG, key="temp/req-1/x.png", content_type="image/png")

    assert "lifecycle" not in client.calls[0]


async def test_fal_failures_propagate_so_the_caller_can_degrade():
    """The backend does not swallow errors: whether a failed upload should
    break the request is the caller's call, not the SDK's."""
    store = _fal_store(_FakeFal(error=RuntimeError("cdn down")))
    with pytest.raises(RuntimeError, match="cdn down"):
        await store.put(PNG, key="temp/req-1/x.png", content_type="image/png")


async def test_fal_ping_refuses_without_a_key():
    """No key means no token fetch, which means no network call to wait on."""
    assert await _fal_store(_FakeFal(), fal_key="").ping() is False


class _FakePost:
    """Just enough of an aiohttp session to see what ping() sends.

    Injecting this is the only way to unit-test ping with a key: without it the
    method builds its own session and the test would hit the real endpoint.
    """

    def __init__(self, status: int = 200):
        self.status = status
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _pinger(http: _FakePost) -> FalStore:
    settings = Settings(storage_backend="fal", fal_key="live-key")
    return FalStore(_FakeFal(), settings, http=http)


async def test_fal_ping_sends_the_json_body_the_token_endpoint_requires():
    """Measured 2026-09-11: without a body the endpoint answers

        422 {"detail":[{"loc":["body"],"msg":"Field required"}]}

    so every health check reported this backend as degraded while uploads
    worked. The SDK sends ``json={}`` for the same reason; this pins that we do
    too, because the failure is silent -- the request simply comes back 4xx.
    """
    http = _FakePost()

    assert await _pinger(http).ping() is True

    call = http.calls[0]
    assert call["json"] == {}, "the token endpoint rejects a bodyless POST"
    assert call["url"].endswith("storage_type=fal-cdn-v3")
    assert call["headers"]["Authorization"] == "Key live-key"
    # Restated from fal_client's CDNTokenManager so the two cannot drift.
    assert call["headers"]["Accept"] == "application/json"
    assert call["headers"]["Content-Type"] == "application/json"


async def test_fal_ping_reports_degraded_when_the_token_request_is_rejected():
    """A 4xx is a real answer, not an exception: health wants the boolean."""
    assert await _pinger(_FakePost(status=422)).ping() is False


# --- the mixin's contract --------------------------------------------------


class _RecordingStore:
    """A port implementation that records calls and can be made to fail."""

    name = "recording"

    def __init__(self, url: str = "https://cdn.test/x.png", error=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._url = url
        self._error = error

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        self.calls.append((key, content_type))
        if self._error is not None:
            raise self._error
        return StoredObject(url=self._url, key=key, visibility="presigned")

    async def ping(self) -> bool:
        return True


def _ctx(store, **overrides) -> AdapterContext:
    settings = Settings(storage_backend="minio", minio_endpoint="", **overrides)
    return AdapterContext(
        "req-42",
        ChannelSpec(upstream_url="https://vendor.test/api"),
        settings,
        storage=store,
    )


async def test_upload_uses_a_request_scoped_key_with_the_sniffed_extension():
    store = _RecordingStore()

    url = await _ctx(store).upload_temp_image(PNG, ext="webp")

    assert url == "https://cdn.test/x.png"
    key, content_type = store.calls[0]
    # <prefix>/<yyyymmdd>/<request-id>/<uuid>.<ext>
    prefix, day, request_id, name = key.split("/")
    assert prefix == "temp"
    assert day == time.strftime("%Y%m%d")
    assert request_id == "req-42"
    assert name.endswith(".webp")
    assert content_type == "image/webp"


async def test_upload_puts_the_day_in_the_key_so_a_lifecycle_rule_can_use_it():
    """The date has to be a stable path segment, else a bucket rule cannot
    expire a day at a time and nobody can find an upload by when it happened."""
    store = _RecordingStore()

    await _ctx(store).upload_temp_image(PNG, ext="png")

    key = store.calls[0][0]
    assert key.split("/")[1] == time.strftime("%Y%m%d")


async def test_upload_takes_the_key_prefix_from_settings():
    """A deployment that already addresses its bucket through a convention of
    its own (cdn/...) should not have to change the engine to keep it."""
    store = _RecordingStore()

    await _ctx(store, storage_key_prefix="cdn").upload_temp_image(PNG, ext="png")

    assert store.calls[0][0].startswith("cdn/")


async def test_an_empty_key_prefix_still_yields_a_usable_key():
    """Empty or "/"-only would otherwise produce "//20260911/...", which some
    S3 implementations treat as a different (empty) bucket path."""
    store = _RecordingStore()

    for value in ("", "/", "///"):
        await _ctx(store, storage_key_prefix=value).upload_temp_image(PNG, ext="png")
        assert store.calls[-1][0].startswith("temp/")


async def test_upload_returns_a_data_uri_when_no_store_is_configured():
    """The documented degradation, and the reason AC-04 can say "must not fail
    the request"."""
    url = await _ctx(None).upload_temp_image(PNG, ext="png")
    assert url.startswith("data:image/png;base64,")


async def test_a_failing_backend_degrades_instead_of_failing_the_request():
    """Every backend's error zoo collapses to one outcome here."""
    store = _RecordingStore(error=RuntimeError("S3Error: SignatureDoesNotMatch"))

    url = await _ctx(store).upload_temp_image(PNG, ext="png")

    assert url.startswith("data:image/png;base64,")


async def test_a_non_url_result_degrades_rather_than_reaching_a_client():
    """A backend that returned a bare key would break every caller that hands
    the value to an upstream as a link, one layer away from the cause."""
    store = _RecordingStore(url="temp/req-42/x.png")

    url = await _ctx(store).upload_temp_image(PNG, ext="png")

    assert url.startswith("data:image/png;base64,")


# --- minio_public: the same bucket, read anonymously -----------------------


def _public_store(client: _FakeMinio, **overrides) -> MinioPublicStore:
    overrides.setdefault("minio_endpoint", "s3.example")
    overrides.setdefault("minio_bucket", "adapter-temp")
    return MinioPublicStore(client, Settings(storage_backend="minio_public", **overrides))


def test_minio_public_backend_builds_a_public_store():
    store = build_storage(
        Settings(storage_backend="minio_public", minio_endpoint="s3.example")
    )

    assert isinstance(store, MinioPublicStore)
    # The name is what /health reports and what selected it, so it must match.
    assert store.name == "minio_public"


async def test_minio_public_returns_an_unsigned_url_that_never_expires():
    """The whole point of this flavour: no signature, so no expiry to reach."""
    client = _FakeMinio()

    stored = await _public_store(client).put(
        PNG, key="temp/req-1/x.png", content_type="image/png"
    )

    assert stored.url == "http://s3.example/adapter-temp/temp/req-1/x.png"
    assert "X-Amz-Signature" not in stored.url
    assert stored.visibility == "public"
    assert stored.expires_at is None
    # Signing is the thing being avoided, so it must not happen at all -- not
    # merely be left out of the URL afterwards.
    assert client.presigns == []
    # The bytes still travel the same S3 call as the presigned flavour.
    assert client.objects[0]["data"] == PNG
    assert client.objects[0]["content_type"] == "image/png"


async def test_minio_public_prefers_the_configured_origin():
    """A bucket behind a CDN is addressed by that host, not by the S3 endpoint
    -- which is why the override exists rather than always deriving."""
    client = _FakeMinio()

    stored = await _public_store(
        client, minio_public_base_url="https://cdn.example/img/"
    ).put(PNG, key="temp/req-1/x.png", content_type="image/png")

    # A trailing slash on the override must not produce a doubled one.
    assert stored.url == "https://cdn.example/img/temp/req-1/x.png"


async def test_minio_public_url_is_https_when_the_endpoint_is_secure():
    stored = await _public_store(_FakeMinio(), minio_secure=True).put(
        PNG, key="k.png", content_type="image/png"
    )

    assert stored.url == "https://s3.example/adapter-temp/k.png"


async def test_minio_public_ping_fails_when_the_bucket_has_no_policy():
    """Uploads would succeed and every link would 403 at the caller. That is
    the worst failure available here: the write path reports nothing wrong, so
    the health probe is the only place it can surface."""
    store = _public_store(_FakeMinio(policy=None))

    assert await store.ping() is False


async def test_minio_public_ping_still_requires_the_bucket_itself():
    store = _public_store(_FakeMinio(bucket_exists=False, policy="{}"))

    assert await store.ping() is False


@pytest.mark.parametrize(
    "policy",
    [
        # The three spellings of "everyone".
        '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject"}]}',
        '{"Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":["s3:GetObject"]}]}',
        '{"Statement":[{"Effect":"Allow","Principal":{"AWS":["*"]},"Action":"s3:*"}]}',
        # A single statement may be an object rather than a list.
        '{"Statement":{"Effect":"Allow","Principal":"*","Action":"*"}}',
    ],
)
async def test_minio_public_ping_accepts_policies_that_allow_anonymous_reads(policy):
    assert await _public_store(_FakeMinio(policy=policy)).ping() is True


@pytest.mark.parametrize(
    "policy",
    [
        # Deny is not Allow, whatever else it says.
        '{"Statement":[{"Effect":"Deny","Principal":"*","Action":"s3:GetObject"}]}',
        # A named principal is not the anonymous one.
        (
            '{"Statement":[{"Effect":"Allow","Principal":'
            '{"AWS":"arn:aws:iam::1:root"},"Action":"s3:GetObject"}]}'
        ),
        # Allow, everyone, but the wrong verb.
        '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:PutObject"}]}',
        "not json at all",
        "{}",
        '{"Statement":[]}',
    ],
)
async def test_minio_public_ping_rejects_policies_that_do_not_grant_reads(policy):
    assert await _public_store(_FakeMinio(policy=policy)).ping() is False


async def test_minio_public_ping_skips_the_policy_when_an_origin_is_configured():
    """With a CDN in front the URLs do not address the bucket at all, so its
    policy says nothing about whether they resolve -- asserting on it would
    call a working deployment degraded."""
    client = _FakeMinio(policy=None)
    store = _public_store(client, minio_public_base_url="https://cdn.example")

    assert await store.ping() is True
    assert client.policy_reads == []


# --- failover: the primary behind a negative cache -------------------------


class _StubStore:
    """A port implementation that fails on demand, for failover tests."""

    def __init__(self, name: str, *, put_error=None, ping_ok: bool = True) -> None:
        self.name = name
        self.puts: list[str] = []
        self.pings = 0
        self.put_error = put_error
        self.ping_ok = ping_ok

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        self.puts.append(key)
        if self.put_error is not None:
            raise self.put_error
        return StoredObject(
            url=f"https://{self.name}.test/{key}", key=key, visibility="presigned"
        )

    async def ping(self) -> bool:
        self.pings += 1
        return self.ping_ok


class _Clock:
    """A monotonic clock a test can move, so no test has to sleep."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _failover(primary, secondary, *, cooldown: float = 30.0, clock=None) -> FallbackStore:
    return FallbackStore(primary, secondary, cooldown=cooldown, clock=clock or _Clock())


async def test_failover_uses_the_primary_when_it_works():
    primary, secondary = _StubStore("primary"), _StubStore("secondary")

    stored = await _failover(primary, secondary).put(
        PNG, key="k.png", content_type="image/png"
    )

    assert stored.url.startswith("https://primary.test/")
    assert secondary.puts == [], "the fallback must not be touched on the happy path"


async def test_failover_uses_the_secondary_when_the_primary_fails():
    primary = _StubStore("primary", put_error=RuntimeError("refused"))
    secondary = _StubStore("secondary")

    stored = await _failover(primary, secondary).put(
        PNG, key="k.png", content_type="image/png"
    )

    assert stored.url.startswith("https://secondary.test/")


async def test_a_parked_primary_is_not_retried_inside_the_cooldown():
    """The whole point of the negative cache: a refused connection costs ~6s,
    so retrying a dead primary on every upload would roughly double the
    latency of every request while it is down."""
    clock = _Clock()
    primary = _StubStore("primary", put_error=RuntimeError("refused"))
    secondary = _StubStore("secondary")
    store = _failover(primary, secondary, cooldown=30.0, clock=clock)

    for i in range(3):
        await store.put(PNG, key=f"{i}.png", content_type="image/png")

    assert len(primary.puts) == 1, "the parked primary was retried"
    assert len(secondary.puts) == 3, "every upload must still be stored"


async def test_the_primary_is_retried_once_the_cooldown_expires():
    clock = _Clock()
    primary = _StubStore("primary", put_error=RuntimeError("refused"))
    store = _failover(primary, _StubStore("secondary"), cooldown=30.0, clock=clock)

    await store.put(PNG, key="1.png", content_type="image/png")
    clock.advance(31)
    await store.put(PNG, key="2.png", content_type="image/png")

    assert len(primary.puts) == 2, "the park must expire, not persist"


async def test_a_recovered_primary_serves_again_and_the_fallback_stands_down():
    clock = _Clock()
    primary = _StubStore("primary", put_error=RuntimeError("refused"))
    secondary = _StubStore("secondary")
    store = _failover(primary, secondary, cooldown=30.0, clock=clock)

    await store.put(PNG, key="1.png", content_type="image/png")  # parks
    clock.advance(31)
    primary.put_error = None
    stored = await store.put(PNG, key="2.png", content_type="image/png")

    assert stored.url.startswith("https://primary.test/")
    assert len(secondary.puts) == 1, "the fallback should not be used after recovery"


async def test_both_failing_propagates_so_the_caller_can_degrade():
    """StorageMixin is the layer that turns a failure into a data URI; hiding it
    here would conceal that both stores are down."""
    primary = _StubStore("primary", put_error=RuntimeError("primary down"))
    secondary = _StubStore("secondary", put_error=RuntimeError("secondary down"))

    with pytest.raises(RuntimeError, match="secondary down"):
        await _failover(primary, secondary).put(
            PNG, key="k.png", content_type="image/png"
        )


async def test_failover_ping_is_true_when_either_side_can_serve():
    assert (
        await _failover(_StubStore("p", ping_ok=False), _StubStore("s")).ping() is True
    )


async def test_failover_ping_is_false_only_when_both_are_down():
    down = _StubStore("p", ping_ok=False)
    also_down = _StubStore("s", ping_ok=False)

    assert await _failover(down, also_down).ping() is False


async def test_failover_ping_probes_the_primary_even_while_it_is_parked():
    """Finding out whether the primary came back is what a probe is for, and in
    a monitored deployment it is what clears the park."""
    clock = _Clock()
    primary = _StubStore("primary", put_error=RuntimeError("down"))
    store = _failover(primary, _StubStore("secondary"), cooldown=300.0, clock=clock)
    await store.put(PNG, key="k.png", content_type="image/png")  # parks for 300s

    await store.ping()

    assert primary.pings == 1, "the park must not suppress the probe"


def test_failover_name_names_both_halves():
    """Log lines have to say which backend served, so neither may be implicit."""
    store = _failover(_StubStore("minio"), _StubStore("fal"))

    assert store.name == "minio+fal"


def test_the_fallback_defaults_to_off():
    assert Settings().storage_fallback_backend == ""
    assert Settings().storage_failover_cooldown == 30


def test_a_self_fallback_is_rejected_at_startup():
    """It would look like failover in the config and in logs while trying the
    same backend twice."""
    with pytest.raises(ValidationError):
        Settings(storage_backend="minio", storage_fallback_backend="minio")


def test_build_storage_wraps_the_primary_when_a_fallback_is_configured():
    # minio -> minio_public on purpose: it exercises the wiring without
    # dragging the fal SDK into a unit test.
    store = build_storage(
        Settings(
            storage_backend="minio",
            minio_endpoint="s3.example",
            storage_fallback_backend="minio_public",
        )
    )

    assert isinstance(store, FallbackStore)
    assert store.name == "minio+minio_public"


def test_an_unconfigured_fallback_leaves_the_primary_alone():
    """Failover that can never fire is worse than none, so it is reported and
    dropped rather than quietly wrapped around nothing."""
    store = build_storage(
        Settings(
            storage_backend="minio",
            minio_endpoint="s3.example",
            storage_fallback_backend="fal",
            fal_key="",
        )
    )

    assert isinstance(store, MinioStore)
    assert not isinstance(store, FallbackStore)


def test_no_fallback_configured_leaves_the_primary_unwrapped():
    store = build_storage(
        Settings(storage_backend="minio", minio_endpoint="s3.example")
    )

    assert isinstance(store, MinioStore)
    assert not isinstance(store, FallbackStore)
