"""volcengine_ark/images@v1: references are materialised concurrently.

The concurrency is the one thing a caller cannot see in the body, which is why
it needs its own file: what it moves is how many requests we keep in flight on
the caller's origin. Peak concurrency is ``min(fanout_concurrency, N)``, so N=1
stays serial (the contract the other channels' tests also pin), N at or below the
cap goes out in one wave, and N above it is bounded rather than merely lucky.

The failure semantics are asserted here too: a fanned-out request must fail with
the earliest failing item's *own* exception, because `ctx.fail()`'s 400 wrapped
in an `ExceptionGroup` reaches the client as a 500.

The stubs await on purpose. Without an await point inside them the tasks cannot
interleave and every peak measurement below would read 1 whatever the script
did -- a false green in the one place this file exists to look.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "volcengine_ark"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ark = _load("ark_images_v1_concurrency", "images@v1.py")


def _real_png(width: int = 400, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


REAL_PNG = _real_png()
REAL_BARE = base64.b64encode(REAL_PNG).decode()

URL = "https://overseas.example/holiday.png"
OTHER_URL = "https://overseas.example/other.png"
REHOSTED = "https://our-storage.example/temp.png"


class _ChannelStub:
    """Only the attributes ContextCore copies. No channel semantics involved."""

    upstream_key = "ark-key"
    upstream_url = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    stage_urls: ClassVar[dict] = {}

    def __init__(self, options: dict) -> None:
        self.options = options


class Spy:
    """What the request did to the client's images, and how many at once.

    ``peak`` is what makes the concurrency assertions worth anything: a body
    that happens to look right must not pass while the references were quietly
    materialised one at a time. It is only measurable because the stubs await.

    ``per_url`` is what lets one reference be distinguishable from another, for
    the input-order test.
    """

    def __init__(self, payload: bytes = REAL_PNG, delay: float = 0.02) -> None:
        self.downloads: list[str] = []
        self.uploads: list[bytes] = []
        self.exts: list[str] = []
        self.payload = payload
        self.per_url: dict[str, bytes] = {}
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def _spend(self, url: str) -> None:
        """The await point that lets the tasks overlap."""
        if self.delay:
            await asyncio.sleep(self.delay)

    async def download(self, url: str) -> bytes:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.downloads.append(url)
        try:
            await self._spend(url)
            return self.per_url.get(url, self.payload)
        finally:
            self.in_flight -= 1

    async def upload(self, data: bytes, ext: str = "png") -> str:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.uploads.append(data)
        self.exts.append(ext)
        try:
            await self._spend("")
            return REHOSTED
        finally:
            self.in_flight -= 1


def _ctx(
    monkeypatch,
    payload: bytes = REAL_PNG,
    concurrency: int | None = None,
    delay: float = 0.02,
    **options,
) -> tuple[AdapterContext, Spy]:
    settings: dict = {"minio_endpoint": ""}
    if concurrency is not None:
        settings["fanout_concurrency"] = concurrency
    ctx = AdapterContext(
        request_id="req-1",
        channel=_ChannelStub(options),
        settings=Settings(**settings),
    )
    spy = Spy(payload, delay)

    async def fake_download(url: str) -> bytes:
        return await spy.download(url)

    async def fake_upload(data: bytes, ext: str = "png") -> str:
        return await spy.upload(data, ext)

    monkeypatch.setattr(ctx, "download_image", fake_download)
    monkeypatch.setattr(ctx, "upload_temp_image", fake_upload)
    return ctx, spy


async def _body(ctx, image=None, prompt: str = "replace the circle", **extra):
    payload = {"prompt": prompt, **extra}
    if image is not None:
        payload["image"] = image
    return await ark.transform(ctx, payload, "request")


async def _image(ctx, image, **extra):
    return (await _body(ctx, image, **extra))["image"]


class TestConcurrentReferences:
    """Peak concurrency on the caller's origin."""

    @pytest.mark.asyncio
    async def test_a_single_reference_takes_the_serial_arm(self, monkeypatch):
        """N=1 must not construct the semaphore: it is the case where a fan-out
        has nothing to overlap, and the one every channel's suite pins."""
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        await _image(ctx, [URL])
        assert spy.peak == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("count", [2, 4, 5])
    async def test_references_go_out_together(self, monkeypatch, count):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        await _image(ctx, [URL] * count)
        assert spy.peak == count
        assert len(spy.downloads) == count

    @pytest.mark.asyncio
    async def test_the_configured_degree_is_a_ceiling_not_a_suggestion(
        self, monkeypatch
    ):
        """Six references with a degree of three: bounded, and still all six."""
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri", concurrency=3)
        await _image(ctx, [URL] * 6)
        assert spy.peak == 3
        assert len(spy.downloads) == 6

    @pytest.mark.asyncio
    async def test_the_default_degree_covers_a_four_reference_edit(
        self, monkeypatch
    ):
        """No `concurrency` argument: whatever `settings` defaults to must cover
        a four-reference edit in one wave, or the default is a regression."""
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        await _image(ctx, [URL] * 4)
        assert spy.peak == 4

    @pytest.mark.asyncio
    async def test_the_uploads_go_out_together_too(self, monkeypatch):
        """The other half of the materialisation, and the one `url` mode takes:
        inline references are re-hosted, so N of them are N uploads."""
        ctx, spy = _ctx(monkeypatch)
        await _image(ctx, [REAL_BARE] * 4)
        assert spy.peak == 4
        assert spy.downloads == []
        assert len(spy.uploads) == 4

    @pytest.mark.asyncio
    async def test_results_keep_input_order_however_they_finish(self, monkeypatch):
        """Two references answering at different speeds, one distinguishable
        result each: the array must be in input order, not completion order."""
        slow, fast = URL, OTHER_URL
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri", delay=0.0)
        small, large = _real_png(40, 30), _real_png(80, 60)
        spy.per_url = {slow: large, fast: small}

        async def download(url: str) -> bytes:
            spy.in_flight += 1
            spy.peak = max(spy.peak, spy.in_flight)
            spy.downloads.append(url)
            try:
                await asyncio.sleep(0.08 if url == slow else 0.0)
                return spy.per_url[url]
            finally:
                spy.in_flight -= 1

        monkeypatch.setattr(ctx, "download_image", download)
        image = await _image(ctx, [slow, fast])
        assert len(image) == 2
        assert base64.b64decode(image[0].split(";base64,", 1)[1]) == large
        assert base64.b64decode(image[1].split(";base64,", 1)[1]) == small

    @pytest.mark.asyncio
    async def test_a_failing_reference_fails_the_request_with_its_own_error(
        self, monkeypatch
    ):
        """A 400 from one reference must not arrive as an `ExceptionGroup`:
        `_call_phase`'s `except AdapterError` would not match it and the client
        would read a 500."""
        ctx, _ = _ctx(monkeypatch, image_ref_mode="data_uri")

        async def refusing(url: str) -> bytes:
            raise AdapterError(400, "refused", "invalid_request_error", None, "nope")

        monkeypatch.setattr(ctx, "download_image", refusing)
        with pytest.raises(AdapterError) as excinfo:
            await _image(ctx, [URL, URL])
        assert excinfo.value.code == "nope"
        assert not isinstance(excinfo.value, BaseExceptionGroup)

    @pytest.mark.asyncio
    async def test_the_first_failure_in_input_order_is_the_one_reported(
        self, monkeypatch
    ):
        """A serial loop fails at the first item it *reaches*, so the concurrent
        analogue is the earliest item in input order -- not the first to finish,
        and not the one with the most interesting error."""
        ctx, _ = _ctx(monkeypatch, image_ref_mode="data_uri")

        async def failing(url: str) -> bytes:
            if url == URL:
                await asyncio.sleep(0.05)
                raise AdapterError(400, "first item", "invalid_request_error", None, "first")
            raise AdapterError(409, "second item", "invalid_request_error", None, "second")

        monkeypatch.setattr(ctx, "download_image", failing)
        with pytest.raises(AdapterError) as excinfo:
            await _image(ctx, [URL, OTHER_URL])
        assert excinfo.value.code == "first"

    @pytest.mark.asyncio
    async def test_text_to_image_never_reaches_the_fan_out(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch)
        await _body(ctx)
        assert spy.peak == 0
        assert spy.downloads == [] and spy.uploads == []


@pytest.mark.parametrize("filename", ["images@v1.py"])
def test_the_new_version_is_within_the_script_sandbox(filename):
    """A script edit the loader would refuse must fail here, not on deploy."""
    from adapter.sandbox import scan_source

    scan_source(
        (ROOT / filename).read_text(encoding="utf-8"),
        filename=f"volcengine_ark/{filename}",
    )
