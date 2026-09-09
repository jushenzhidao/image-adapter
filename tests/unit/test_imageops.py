"""Unit tests for ctx.image (BR-012, AC-23).

The security-relevant cases here are the decompression bomb and the format
allowlist: both are reachable with attacker-supplied bytes.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from adapter.errors import InvalidRequestError
from adapter.settings import Settings
from adapter.utils.imageops import ImageOps


def png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        adapter_key="k",
        redis_url="",
        minio_endpoint="",
        max_image_pixels=1_000_000,
        max_asset_bytes=2 * 1024 * 1024,
    )


@pytest.fixture
def ops(settings) -> ImageOps:
    return ImageOps(settings)


def run(coro):
    return asyncio.run(coro)


class TestInfo:
    def test_reports_dimensions_and_format(self, ops):
        meta = run(ops.info(png(320, 240)))
        assert meta["width"] == 320
        assert meta["height"] == 240
        assert meta["format"] == "PNG"

    def test_empty_payload_is_rejected(self, ops):
        with pytest.raises(InvalidRequestError):
            run(ops.info(b""))

    def test_non_image_bytes_are_rejected(self, ops):
        with pytest.raises(InvalidRequestError):
            run(ops.info(b"this is not an image"))


class TestResize:
    def test_keep_ratio_fits_inside_the_box_without_distorting(self, ops):
        out = run(ops.resize(png(800, 600), 200, 200))
        meta = run(ops.info(out))
        # 4:3 fitted into 200x200 gives 200x150, not a squashed 200x200.
        assert (meta["width"], meta["height"]) == (200, 150)

    def test_keep_ratio_false_forces_exact_dimensions(self, ops):
        out = run(ops.resize(png(800, 600), 200, 200, keep_ratio=False))
        meta = run(ops.info(out))
        assert (meta["width"], meta["height"]) == (200, 200)

    def test_upscaling_is_not_forced_when_keeping_ratio(self, ops):
        # thumbnail() leaves an already-smaller image alone, which is what a
        # "fit within" preprocess step should do.
        out = run(ops.resize(png(100, 80), 400, 400))
        meta = run(ops.info(out))
        assert (meta["width"], meta["height"]) == (100, 80)

    @pytest.mark.parametrize("width,height", [(0, 100), (-5, 100), (100, 0)])
    def test_nonpositive_dimensions_are_rejected(self, ops, width, height):
        with pytest.raises(InvalidRequestError):
            run(ops.resize(png(50, 50), width, height))

    def test_absurd_target_is_rejected_before_allocating(self, ops):
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.resize(png(50, 50), 40_000, 40_000))
        assert exc.value.code == "image_too_large"


class TestDecompressionBomb:
    def test_oversized_image_is_refused(self, ops):
        # 2000x2000 = 4 MP against a 1 MP ceiling. Small file, large raster.
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.info(png(2000, 2000)))
        assert exc.value.code == "image_too_large"

    def test_bomb_is_refused_by_resize_too(self, ops):
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.resize(png(2000, 2000), 64, 64))
        assert exc.value.code == "image_too_large"

    def test_image_just_under_the_ceiling_still_works(self, ops):
        meta = run(ops.info(png(900, 900)))  # 0.81 MP
        assert meta["width"] == 900


class TestConvert:
    def test_png_to_jpeg(self, ops):
        out = run(ops.convert(png(120, 120), "jpeg"))
        assert run(ops.info(out))["format"] == "JPEG"

    def test_rgba_source_is_flattened_for_jpeg(self, ops):
        # JPEG has no alpha channel; without an explicit convert Pillow raises.
        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 128)).save(buf, format="PNG")
        out = run(ops.convert(buf.getvalue(), "jpg"))
        assert run(ops.info(out))["format"] == "JPEG"

    def test_unsupported_target_format_is_rejected(self, ops):
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.convert(png(64, 64), "tiff"))
        assert exc.value.code == "image_format_unsupported"

    @pytest.mark.parametrize("quality", [0, 101, 500])
    def test_out_of_range_quality_is_rejected(self, ops, quality):
        with pytest.raises(InvalidRequestError):
            run(ops.convert(png(64, 64), "jpeg", quality=quality))

    def test_quality_affects_output_size(self, ops):
        src = png(400, 400)
        small = run(ops.convert(src, "jpeg", quality=20))
        large = run(ops.convert(src, "jpeg", quality=95))
        assert len(small) < len(large)


class TestOutputCap:
    def test_result_over_the_byte_cap_is_refused(self):
        tight = Settings(
            adapter_key="k",
            redis_url="",
            minio_endpoint="",
            max_image_pixels=50_000_000,
            max_asset_bytes=1024,  # 1 KB: any real image busts this
        )
        with pytest.raises(InvalidRequestError) as exc:
            run(ImageOps(tight).resize(png(1200, 1200), 1000, 1000, keep_ratio=False))
        assert exc.value.code == "image_too_large"


class TestDataUrl:
    def test_emits_a_correctly_typed_data_url(self, ops):
        url = run(ops.to_data_url(png(32, 32)))
        assert url.startswith("data:image/png;base64,")


def test_operators_do_not_block_the_event_loop():
    """CPU work must run in a thread, or one resize stalls every request."""

    async def scenario():
        settings = Settings(
            adapter_key="k",
            redis_url="",
            minio_endpoint="",
            max_image_pixels=50_000_000,
            max_asset_bytes=20 * 1024 * 1024,
        )
        ops = ImageOps(settings)
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.001)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        # Large enough that a synchronous resize would visibly stall the loop.
        await ops.resize(png(3000, 3000), 600, 600)
        beat.cancel()
        return ticks

    # If resize ran inline the heartbeat would never have been scheduled.
    assert asyncio.run(scenario()) > 0
