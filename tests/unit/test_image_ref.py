"""``ctx.image_b64``: the three-shape normalisation and the round trip it dropped.

The previous implementation decoded and then re-encoded any non-URL input,
which rebuilt a string identical to the input at a cost of 25 ms of pure CPU
per 20 MB image -- on the event loop, and once per reference, so an
image-to-image request with four references paid ~100 ms of it.

These tests pin the two halves that matter: the fast path must produce exactly
what the round trip produced, and it must not become a way to skip the
validation or the byte cap that justified decoding in the first place.
"""

from __future__ import annotations

import base64
from typing import ClassVar

import pytest

from adapter.context import AdapterContext
from adapter.errors import InvalidRequestError
from adapter.settings import Settings


class _ChannelStub:
    """Only the attributes ContextCore copies. No channel semantics involved."""

    options: ClassVar[dict] = {}
    upstream_key = "vendor-key"
    upstream_url = "https://upstream.example/v1"
    stage_urls: ClassVar[dict] = {}


def _ctx(**overrides) -> AdapterContext:
    """The fully composed context: ctx.image_b64 comes from a mixin, so
    ContextCore alone (which only owns infra and state) does not have it."""
    return AdapterContext(
        request_id="req-1",
        channel=_ChannelStub(),
        settings=Settings(minio_endpoint="", **overrides),
    )


def _round_trip(ref: str) -> str:
    """What ``image_b64`` used to do, kept verbatim as the oracle."""
    value = ref
    if value.startswith("data:") and ";base64," in value:
        value = value.split(";base64,", 1)[1]
    return base64.b64encode(base64.b64decode(value, validate=True)).decode("ascii")


PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"
BARE = base64.b64encode(PNG).decode()
DATA_URI = f"data:image/png;base64,{BARE}"


@pytest.mark.asyncio
async def test_bare_base64_is_handed_back_unchanged():
    assert await _ctx().image_b64(BARE) == BARE


@pytest.mark.asyncio
async def test_data_uri_is_reduced_to_its_payload():
    assert await _ctx().image_b64(DATA_URI) == BARE


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [BARE, DATA_URI])
async def test_output_matches_the_round_trip_it_replaced(ref):
    assert await _ctx().image_b64(ref) == _round_trip(ref)


@pytest.mark.asyncio
async def test_a_url_is_still_downloaded_and_encoded(monkeypatch):
    ctx = _ctx()
    seen: list[str] = []

    async def fake_download(url: str) -> bytes:
        seen.append(url)
        return PNG

    monkeypatch.setattr(ctx, "download_image", fake_download)
    assert await ctx.image_b64("https://cdn.example/a.png") == BARE
    assert seen == ["https://cdn.example/a.png"]


@pytest.mark.asyncio
async def test_malformed_base64_is_still_refused():
    """The fast path must not become a way around the alphabet check."""
    with pytest.raises(InvalidRequestError):
        await _ctx().image_b64("this is not base64 at all !!!")


@pytest.mark.asyncio
async def test_oversized_payload_is_still_refused():
    """Skipping the re-encode must not skip the cap either."""
    ctx = _ctx(max_asset_bytes=16)
    too_big = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(InvalidRequestError):
        await ctx.image_b64(too_big)


@pytest.mark.asyncio
async def test_payload_exactly_at_the_cap_is_accepted():
    ctx = _ctx(max_asset_bytes=len(PNG))
    assert await ctx.image_b64(BARE) == BARE
