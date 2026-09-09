"""Unit tests for image output normalization helpers."""

import base64

import pytest

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.settings import Settings
from adapter.utils.image import is_probably_base64, strip_data_uri, to_output_item

PNG_1X1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="


def _ctx() -> AdapterContext:
    settings = Settings(redis_url="", minio_endpoint="")
    channel = ChannelSpec(upstream_url="https://vendor.test/api")
    return AdapterContext("req-1", channel, settings, endpoint="images")


def test_strip_data_uri():
    data_uri = f"data:image/png;base64,{PNG_1X1_B64}"
    assert strip_data_uri(data_uri) == PNG_1X1_B64
    assert strip_data_uri(PNG_1X1_B64) == PNG_1X1_B64


def test_is_probably_base64():
    assert is_probably_base64(PNG_1X1_B64)
    assert is_probably_base64(f"data:image/png;base64,{PNG_1X1_B64}")
    assert not is_probably_base64("not-base64!")


@pytest.mark.asyncio
async def test_to_output_item_b64_from_raw():
    """When the client wants b64_json, raw bytes are encoded."""
    ctx = _ctx()
    raw_bytes = base64.b64decode(PNG_1X1_B64)
    item = await to_output_item(ctx, "b64_json", raw=raw_bytes)

    assert item["b64_json"] == PNG_1X1_B64
    await ctx.close()


@pytest.mark.asyncio
async def test_to_output_item_url_from_raw_degraded():
    """URL requested + raw bytes + no MinIO -> data URI degradation."""
    ctx = _ctx()
    raw_bytes = base64.b64decode(PNG_1X1_B64)
    item = await to_output_item(ctx, "url", raw=raw_bytes)

    assert item["url"].startswith("data:image/png;base64,")
    await ctx.close()
