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
import io
from typing import ClassVar

import pytest
from PIL import Image

from adapter.context import AdapterContext
from adapter.errors import InvalidRequestError, UpstreamError
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


# ``download_image`` is the one entry point that does not dispatch on shape,
# and its name is what makes vision scripts reach for it with inline data.
# These pin the guard that turns that mistake into a self-correcting message.


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", [BARE, DATA_URI])
async def test_download_image_refuses_inline_references(ref):
    """Both inline shapes are named as such, and the right helper is named.

    Before the guard this came out as ``channel_config_error`` -- "the channel
    headers are unusable" -- which points the reader at the control plane for
    what is a script bug.
    """
    with pytest.raises(InvalidRequestError) as excinfo:
        await _ctx().download_image(ref)

    assert "image_bytes" in str(excinfo.value)
    assert excinfo.value.code != "channel_config_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", "   ", None, 42])
async def test_download_image_refuses_non_string_references(ref):
    with pytest.raises(InvalidRequestError):
        await _ctx().download_image(ref)


@pytest.mark.asyncio
async def test_download_image_does_not_echo_the_payload():
    """The rejected value is usually megabytes of base64; an error body is not
    the place to hand it back."""
    with pytest.raises(InvalidRequestError) as excinfo:
        await _ctx().download_image("x" * 4096)

    assert len(str(excinfo.value)) < 400


@pytest.mark.asyncio
async def test_download_image_leaves_http_urls_to_the_ssrf_guard(monkeypatch):
    """The shape guard must not shadow the existing URL checks."""
    reached = RuntimeError("check_url reached")

    def fake_check(raw, settings, header="X-Upstream-Url"):
        raise reached

    monkeypatch.setattr("adapter.ctxapi.image_ref.check_url", fake_check)
    with pytest.raises(RuntimeError) as excinfo:
        await _ctx().download_image("https://cdn.example/a.png")
    assert excinfo.value is reached


# `compress_image` is the bytes-out counterpart of `image_url`: a script that
# needs a smaller reference should not have to know which of the three shapes
# it arrived in, nor call the codec helpers in the right order. What it must
# *not* do is decide anything -- the limits are arguments, and refusing an
# image is left to the layer that can tell a client error from a channel one.


def real_png(width: int = 400, height: int = 300) -> bytes:
    """A payload Pillow can actually open, unlike this module's PNG stub."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def png_side(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


@pytest.mark.asyncio
async def test_compress_image_applies_the_ceiling_to_an_inline_reference():
    src = real_png()
    ref = f"data:image/png;base64,{base64.b64encode(src).decode()}"
    out = await _ctx().compress_image(ref, max_edge=100)
    assert png_side(out) == (100, 75)
    assert len(out) < len(src)


@pytest.mark.asyncio
async def test_compress_image_accepts_a_bare_base64_reference():
    src = real_png()
    out = await _ctx().compress_image(base64.b64encode(src).decode(), max_edge=100)
    assert png_side(out) == (100, 75)


@pytest.mark.asyncio
async def test_compress_image_fetches_a_url_exactly_once(monkeypatch):
    """One download per reference: the fetch and the compression are one call.

    A second pass over the same URL would double the budget this operator
    exists to spend, and the URL is the one whose latency is not ours.
    """
    ctx = _ctx()
    seen: list[str] = []
    src = real_png()

    async def fake_download(url: str) -> bytes:
        seen.append(url)
        return src

    monkeypatch.setattr(ctx, "download_image", fake_download)
    out = await ctx.compress_image("https://cdn.example/a.png", max_edge=100)
    assert seen == ["https://cdn.example/a.png"]
    assert png_side(out) == (100, 75)


@pytest.mark.asyncio
async def test_compress_image_keeps_the_input_byte_cap():
    """Compressing is not a way to be handed more than the cap allows."""
    ctx = _ctx(max_asset_bytes=64)
    ref = f"data:image/png;base64,{base64.b64encode(real_png()).decode()}"
    with pytest.raises(InvalidRequestError):
        await ctx.compress_image(ref, max_edge=100)


@pytest.mark.asyncio
async def test_compress_image_refuses_bytes_that_are_not_an_image():
    with pytest.raises(InvalidRequestError) as excinfo:
        await _ctx().compress_image(base64.b64encode(b"junk").decode(), max_edge=100)
    assert excinfo.value.code == "image_invalid"


@pytest.mark.asyncio
async def test_compress_image_refuses_an_unknown_target_format():
    ref = f"data:image/png;base64,{base64.b64encode(real_png()).decode()}"
    with pytest.raises(InvalidRequestError) as excinfo:
        await _ctx().compress_image(ref, fmt="tiff")
    assert excinfo.value.code == "image_format_unsupported"


@pytest.mark.asyncio
async def test_compress_image_refuses_a_non_string_reference():
    with pytest.raises(InvalidRequestError):
        await _ctx().compress_image("")


# ------------------------------------------- the download verdict is on the bytes
#
# The gate used to read `Content-Type` alone, which is wrong in the direction
# that costs: measured 2026-09-18 (qwen, reproduced by the reference project),
# a genuine PNG arrives as `application/octet-stream`. The verdict now needs
# the header *and* the bytes to disagree with "image".


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    async def iter_chunked(self, size: int):
        yield self._data


class _FakeDownload:
    """The slice of an aiohttp response `_fetch_capped` touches."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self.status = 200
        self.headers = headers
        self.content_length = len(body)
        self.content = _FakeBody(body)


class _FakeSession:
    def __init__(self, resp):
        self.resp = resp
        self.closed = False   # `ContextCore.http` treats a closed session as absent

    def get(self, url):
        return _FakeCM(self.resp)


class _FakeCM:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *exc):
        return False


def _ctx_with(resp) -> AdapterContext:
    return AdapterContext(
        request_id="req-1",
        channel=_ChannelStub(),
        settings=Settings(minio_endpoint=""),
        http=_FakeSession(resp),
    )


@pytest.mark.asyncio
async def test_a_real_image_labelled_octet_stream_is_not_refused():
    """真图可能是 `application/octet-stream`（实测）—— 按头判会把好图误杀。"""
    data = real_png()
    ctx = _ctx_with(_FakeDownload(data, {"Content-Type": "application/octet-stream"}))
    assert await ctx.download_image("https://cdn.example/a.png") == data


@pytest.mark.asyncio
async def test_an_html_error_page_is_still_refused():
    """死链/挑战页没有图片 magic ⇒ 仍要拒（这是这条判据的正当用途）。"""
    ctx = _ctx_with(_FakeDownload(b"<!doctype html><title>404</title>",
                                  {"Content-Type": "text/html; charset=utf-8"}))
    with pytest.raises(UpstreamError) as excinfo:
        await ctx.download_image("https://cdn.example/a.png")
    assert excinfo.value.code == "image_content_type"


@pytest.mark.asyncio
async def test_an_image_header_is_still_enough_on_its_own():
    data = real_png()
    ctx = _ctx_with(_FakeDownload(data, {"Content-Type": "image/png"}))
    assert await ctx.download_image("https://cdn.example/a.png") == data


@pytest.mark.asyncio
async def test_a_missing_header_keeps_being_accepted():
    """空头本来就放行（旧行为），改判据不该把它变成新拒绝。"""
    data = b"no-magic-but-the-header-said-nothing"
    ctx = _ctx_with(_FakeDownload(data, {}))
    assert await ctx.download_image("https://cdn.example/a.png") == data
