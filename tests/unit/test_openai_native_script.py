"""The shipped ``openai/images@v1`` script, driven directly.

Gate one of two -- the other is
``tests/integration/test_fanout_materialisation.py``. This one loads the file by
path with a **real** ``AdapterContext``, so every mixin is real and only the
network and the object store are stood in for. What this layer sees that the
end-to-end one cannot cheaply is *dispatch*: which endpoint, which encoding, one
part or a bracketed array -- read off the plan the script emits rather than off
the bytes on the wire, which makes a wrong answer point at the line that chose
it.

The fan-out itself is asserted here too, but as a counter rather than as timing:
the point is that the references overlap, and a fake downloader can say that
exactly.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import pathlib

import pytest

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.sandbox import scan_source
from adapter.settings import Settings

SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "script_store"
    / "openai"
    / "images@v1.py"
)

UPSTREAM = "https://vendor.test/v1/images/generations"
EDITS = "https://vendor.test/v1/images/edits"

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _load():
    """The script module, loaded from the file the image ships."""
    spec = importlib.util.spec_from_file_location("openai_images_v1", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load()


class _Downloads:
    """Stands in for ``ctx.download_image`` and counts overlap."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.urls: list[str] = []

    async def __call__(self, url: str) -> bytes:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.urls.append(url)
        try:
            await asyncio.sleep(self.delay)
            return PNG
        finally:
            self.in_flight -= 1


def _ctx() -> AdapterContext:
    settings = Settings(
        environment="dev",
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        fal_key="",
    )
    channel = ChannelSpec(upstream_url=UPSTREAM)
    return AdapterContext("v1-test", channel, settings, endpoint="images")


def _wire(monkeypatch, ctx) -> _Downloads:
    """Replaces the one network entry point, leaving every mixin real."""
    recorder = _Downloads()
    monkeypatch.setattr(ctx, "download_image", recorder)
    return recorder


def _parts(ctx) -> list:
    assert ctx.plan.files is not None
    return [part for parts in ctx.plan.files.values() for part in parts]


# --- the script is still inside the sandbox policy -------------------------


def test_the_shipped_text_still_passes_the_sandbox_scan():
    """A script that trips the policy is rejected at load, in production only.
    Cheap to assert here, and it would otherwise be discovered by an outage."""
    scan_source(SCRIPT_PATH.read_text(encoding="utf-8"), filename="openai/images@v1.py")


# --- dispatch, read off the plan ------------------------------------------


async def test_three_references_become_one_bracketed_batch_in_order(script, monkeypatch):
    ctx = _ctx()
    _wire(monkeypatch, ctx)
    refs = [f"https://refs.test/{i}.png" for i in range(3)]

    body = await script.transform(ctx, {"prompt": "x", "image": refs}, "request")

    assert ctx.plan.url == EDITS
    assert list(ctx.plan.files) == ["image[]"]
    assert [part[0] for part in _parts(ctx)] == [
        "image0.png",
        "image1.png",
        "image2.png",
    ]
    # The picture fields become parts and are not also sent as text.
    assert "image" not in body


async def test_a_single_reference_uses_the_bare_field_name(script, monkeypatch):
    ctx = _ctx()
    recorder = _wire(monkeypatch, ctx)

    await script.transform(ctx, {"prompt": "x", "image": "https://refs.test/0.png"}, "request")

    assert list(ctx.plan.files) == ["image"]
    assert recorder.peak == 1, "one reference must not go through the fan-out machinery"


async def test_a_mask_rides_in_the_same_batch(script, monkeypatch):
    """The mask is an independent read of the same kind, so it is fetched
    alongside the references rather than after them -- and it still lands under
    its own field name."""
    ctx = _ctx()
    recorder = _wire(monkeypatch, ctx)

    await script.transform(
        ctx,
        {
            "prompt": "x",
            "image": "https://refs.test/0.png",
            "mask": "https://refs.test/mask.png",
        },
        "request",
    )

    assert list(ctx.plan.files) == ["image", "mask"]
    assert len(recorder.urls) == 2
    assert recorder.peak > 1, "the mask was fetched after the reference, not with it"


async def test_a_text_to_image_request_emits_no_plan(script, monkeypatch):
    ctx = _ctx()
    _wire(monkeypatch, ctx)

    body = await script.transform(ctx, {"prompt": "x"}, "request")

    assert ctx.plan.url is None and ctx.plan.files is None
    assert body == {"prompt": "x"}


# --- dispatch on the way back ---------------------------------------------


async def test_reply_items_are_converted_concurrently(script, monkeypatch):
    """The outbound half: N items each need their picture fetched, and the
    caller gets one b64 per item in the vendor's order."""
    ctx = _ctx()
    recorder = _wire(monkeypatch, ctx)

    await script.transform(
        ctx, {"prompt": "x", "response_format": "b64_json"}, "request"
    )
    reply = {"created": 1, "data": [{"url": f"https://cdn.test/{i}.png"} for i in range(3)]}

    out = await script.transform(ctx, reply, "response")

    assert recorder.peak > 1, "the reply items were converted one at a time"
    assert [base64.b64decode(item["b64_json"]) for item in out["data"]] == [PNG] * 3
    assert [list(item) for item in out["data"]] == [["b64_json"]] * 3


async def test_a_single_reply_item_is_converted_serially(script, monkeypatch):
    ctx = _ctx()
    recorder = _wire(monkeypatch, ctx)

    await script.transform(
        ctx, {"prompt": "x", "response_format": "b64_json"}, "request"
    )
    await script.transform(ctx, {"data": [{"url": "https://cdn.test/0.png"}]}, "response")

    assert recorder.peak == 1


# --- rehost_url: the upstream's own link, fetched and re-stored ------------


class _Stores:
    """Stands in for ``ctx.upload_temp_image`` and records what it stored."""

    def __init__(self, link: str = "https://cdn.test/stored.png") -> None:
        self.link = link
        self.calls: list[tuple[bytes, str]] = []

    async def __call__(self, data: bytes, ext: str = "png") -> str:
        self.calls.append((data, ext))
        return self.link


def _wire_store(monkeypatch, ctx) -> _Stores:
    recorder = _Stores()
    monkeypatch.setattr(ctx, "upload_temp_image", recorder)
    return recorder


async def test_an_upstream_link_is_rehosted_when_the_option_is_on(script, monkeypatch):
    """`rehost_url: true`: the vendor's link is fetched and re-stored as ours."""
    ctx = _ctx()
    ctx.options["rehost_url"] = True
    downloads = _wire(monkeypatch, ctx)
    stores = _wire_store(monkeypatch, ctx)

    await script.transform(ctx, {"prompt": "x", "response_format": "url"}, "request")
    link = "https://vendor-cdn.test/a.png"
    out = await script.transform(
        ctx, {"created": 1, "data": [{"url": link, "width": 3}]}, "response"
    )

    item = out["data"][0]
    assert item["url"] == "https://cdn.test/stored.png"
    assert item["width"] == 3, "extras must ride along"
    assert downloads.urls == [link], "the vendor link must be fetched once"
    assert stores.calls == [(PNG, "png")]


async def test_an_upstream_link_passes_through_without_the_option(script, monkeypatch):
    """Off by default: honouring the upstream's own link costs nothing."""
    ctx = _ctx()
    downloads = _wire(monkeypatch, ctx)
    stores = _wire_store(monkeypatch, ctx)

    await script.transform(ctx, {"prompt": "x", "response_format": "url"}, "request")
    link = "https://vendor-cdn.test/a.png"
    out = await script.transform(ctx, {"data": [{"url": link}]}, "response")

    assert out["data"][0]["url"] == link
    assert downloads.urls == [] and stores.calls == []


async def test_a_hand_written_option_value_does_not_turn_rehosting_on(
    script, monkeypatch
):
    """Only the JSON boolean counts -- "true" in quotes stays off."""
    ctx = _ctx()
    ctx.options["rehost_url"] = "true"
    downloads = _wire(monkeypatch, ctx)
    stores = _wire_store(monkeypatch, ctx)

    await script.transform(ctx, {"prompt": "x", "response_format": "url"}, "request")
    link = "https://vendor-cdn.test/a.png"
    out = await script.transform(ctx, {"data": [{"url": link}]}, "response")

    assert out["data"][0]["url"] == link
    assert downloads.urls == [] and stores.calls == []


async def test_a_rehost_without_object_storage_passes_the_vendor_link_through(
    script, monkeypatch
):
    """No MinIO: the mechanism answers None and the vendor's own link rides
    through. A data URI is never dressed up as `url`, and a 502 out of *our*
    missing storage would fail requests that used to work."""
    ctx = _ctx()
    ctx.options["rehost_url"] = True

    async def degraded_upload(data: bytes, ext: str = "png") -> str:
        return "data:image/png;base64," + base64.b64encode(data).decode()

    monkeypatch.setattr(ctx, "download_image", _Downloads())
    monkeypatch.setattr(ctx, "upload_temp_image", degraded_upload)

    await script.transform(ctx, {"prompt": "x", "response_format": "url"}, "request")
    link = "https://vendor-cdn.test/a.png"
    out = await script.transform(ctx, {"data": [{"url": link}]}, "response")

    assert out["data"][0]["url"] == link
