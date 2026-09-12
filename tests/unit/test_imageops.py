"""Unit tests for ctx.image (BR-012, AC-23).

The security-relevant cases here are the decompression bomb and the format
allowlist: both are reachable with attacker-supplied bytes.
"""

from __future__ import annotations

import asyncio
import io
import random

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


def gradient(width: int, height: int, fmt: str = "PNG", quality: int | None = None) -> bytes:
    """A picture with structure, so its encodings are neither trivial nor huge.

    A flat fill would be the worst possible fixture for a byte-target test: it
    compresses to nothing at every quality, so no ladder would ever run.
    """
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        for x in range(width):
            px[x, y] = ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
    buf = io.BytesIO()
    if quality is None:
        img.save(buf, format=fmt)
    else:
        img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def noisy_png(width: int, height: int) -> bytes:
    """A PNG that resists compression, for the targets it cannot reach.

    The point of a fixed seed is that the byte count is a property of the
    fixture rather than of the run, so a threshold in a test below is a
    statement about behaviour and not a coin flip.
    """
    rnd = random.Random(7)
    img = Image.new("RGB", (width, height))
    img.putdata(
        [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(width * height)
        ]
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def jpeg_source(width: int = 800, height: int = 600) -> bytes:
    return gradient(width, height, fmt="JPEG")


class TestCompress:
    """AC-23's fifth operator: the one that takes a *goal* rather than an
    instruction, which is why most of these tests are about what it refuses to
    spend on the way to that goal.
    """

    def test_max_edge_scales_the_longest_edge_down(self, ops):
        out = run(ops.compress(png(800, 600), max_edge=200))
        meta = run(ops.info(out))
        assert (meta["width"], meta["height"]) == (200, 150)

    def test_max_edge_leaves_an_image_already_inside_the_box_alone(self, ops):
        # A ceiling, not a target: no upscaling, no distortion.
        out = run(ops.compress(png(100, 80), max_edge=400))
        meta = run(ops.info(out))
        assert (meta["width"], meta["height"]) == (100, 80)

    @pytest.mark.parametrize("bad", [0, -1, True, 1.5, "8"])
    def test_a_ceiling_that_is_not_a_positive_int_is_rejected(self, ops, bad):
        # "8" is refused too: an option string that happens to be numeric is
        # still a mistyped option, and guessing on the caller's behalf is how
        # a size limit turns out to be no limit at all.
        with pytest.raises(InvalidRequestError):
            run(ops.compress(png(64, 64), max_edge=bad))

    def test_a_byte_target_that_is_not_a_positive_int_is_rejected(self, ops):
        with pytest.raises(InvalidRequestError):
            run(ops.compress(png(64, 64), max_bytes=True))

    def test_format_conversion(self, ops):
        out = run(ops.compress(png(400, 300), fmt="webp"))
        assert run(ops.info(out))["format"] == "WEBP"

    def test_unknown_target_format_is_rejected(self, ops):
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.compress(png(64, 64), fmt="tiff"))
        assert exc.value.code == "image_format_unsupported"

    @pytest.mark.parametrize("quality", [0, 101, 500])
    def test_out_of_range_quality_is_rejected(self, ops, quality):
        with pytest.raises(InvalidRequestError):
            run(ops.compress(png(64, 64), quality=quality))

    @pytest.mark.parametrize("quality", [True, 85.5])
    def test_a_quality_that_is_not_an_int_is_rejected(self, ops, quality):
        with pytest.raises(InvalidRequestError):
            run(ops.compress(png(64, 64), quality=quality))

    def test_the_default_quality_is_the_stated_85(self, ops):
        """An unstated quality must not silently mean Pillow's 75.

        Asserted as equality with the explicit number, so the constant can
        change in one place and the test still describes it.
        """
        src = jpeg_source()
        assert run(ops.compress(src, quality=85)) == run(ops.compress(src))

    def test_a_byte_target_is_met_by_quality_before_pixels(self, ops):
        """The cheap lever is spent first, and the dimensions survive it."""
        src = jpeg_source()
        out = run(ops.compress(src, max_bytes=60_000))
        meta = run(ops.info(out))
        assert len(out) <= 60_000
        assert len(out) < len(src)
        assert (meta["width"], meta["height"]) == (800, 600)

    def test_pixels_are_spent_when_they_are_what_meets_the_target(self, ops):
        src = jpeg_source()
        out = run(ops.compress(src, max_bytes=40_000))
        meta = run(ops.info(out))
        assert len(out) <= 40_000
        assert meta["width"] * meta["height"] < 800 * 600

    def test_an_unreachable_target_keeps_the_dimensions(self, ops):
        """The worst of both worlds is refused: shrink hard and miss anyway.

        A target this image cannot reach comes back at full size, so the caller
        gets something honest -- "this is as far as it goes" -- rather than a
        picture that lost most of its pixels and still broke their ceiling.
        """
        src = jpeg_source()
        out = run(ops.compress(src, max_bytes=1_000))
        meta = run(ops.info(out))
        assert len(out) > 1_000
        assert (meta["width"], meta["height"]) == (800, 600)

    def test_a_png_reaches_a_byte_target_by_scaling(self, ops):
        """Pixels are the only lever a quality-less format has.

        Without this ladder `max_bytes` would be unreachable for exactly the
        file an operator usually wants shrunk: a screenshot-sized PNG.
        """
        src = noisy_png(300, 300)
        out = run(ops.compress(src, max_bytes=40_000))
        meta = run(ops.info(out))
        assert len(out) <= 40_000
        assert meta["width"] * meta["height"] < 300 * 300

    def test_a_png_target_it_cannot_reach_keeps_the_original_dimensions(self, ops):
        src = noisy_png(300, 300)
        out = run(ops.compress(src, max_bytes=500))
        meta = run(ops.info(out))
        assert len(out) > 500
        assert (meta["width"], meta["height"]) == (300, 300)

    def test_a_target_the_caller_quality_already_meets_is_left_alone(self, ops):
        """Nothing is spent when nothing needs to be.

        The first encode already fits, so no rung is climbed and the answer is
        exactly the encode that was asked for. Spending CPU -- and quality --
        to "improve" a request that was already satisfied is how a phase budget
        disappears.
        """
        src = jpeg_source()
        offered = run(ops.compress(src, quality=60))
        out = run(ops.compress(src, quality=60, max_bytes=len(offered)))
        assert out == offered

    def test_the_ladder_skips_the_rungs_the_caller_did_not_offer(
        self, ops, monkeypatch
    ):
        """A request for 60 is never re-encoded at 70 on the way down.

        Counted rather than compared, because the *result* is the same either
        way -- what the guard buys is the CPU the wasted rungs would have spent,
        and on a 20 MB reference that is not a rounding error. A byte-level
        assertion here would pass with the guard removed, which is exactly the
        kind of test this project tries not to write.
        """
        seen: list = []
        original = ops._encode

        def counting(img, fmt, **params):
            seen.append(params.get("quality"))
            return original(img, fmt, **params)

        monkeypatch.setattr(ops, "_encode", counting)
        # Unreachable, so the whole ladder is walked.
        run(ops.compress(jpeg_source(), quality=60, max_bytes=1_000))
        assert 70 not in seen
        assert 55 in seen

    def test_a_same_format_re_encode_that_grows_is_discarded(self, ops):
        """Asking for the format an image already has must not inflate it.

        Converting a JPEG saved at quality 30 back to JPEG at 85 makes it far
        larger, and a channel that set `ref_fmt: "jpeg"` is asking for the
        format, not for a bigger upload.
        """
        src = gradient(800, 600, fmt="JPEG", quality=30)
        assert run(ops.compress(src, fmt="jpeg")) == src
        assert run(ops.compress(src)) == src

    def test_a_format_change_is_honoured_even_when_it_is_larger(self, ops):
        """The other half of that rule, pinned so it cannot drift into it.

        A caller may name a format because the upstream only accepts that one,
        in which case returning "the smaller original" would fail the request
        instead of the size goal.
        """
        # A gradient PNG compresses extremely well, so WEBP comes out larger.
        src = gradient(800, 600, fmt="PNG")
        out = run(ops.compress(src, fmt="webp"))
        assert run(ops.info(out))["format"] == "WEBP"
        assert len(out) > len(src)

    def test_non_image_bytes_are_rejected(self, ops):
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.compress(b"this is not an image"))
        assert exc.value.code == "image_invalid"

    def test_the_decompression_bomb_guard_still_applies(self, ops):
        # 4 MP against the fixture's 1 MP ceiling, reached before any scaling.
        with pytest.raises(InvalidRequestError) as exc:
            run(ops.compress(png(2000, 2000), max_edge=64))
        assert exc.value.code == "image_too_large"

    def test_the_output_byte_cap_still_applies(self):
        tight = Settings(
            adapter_key="k",
            redis_url="",
            minio_endpoint="",
            max_image_pixels=50_000_000,
            max_asset_bytes=1024,
        )
        with pytest.raises(InvalidRequestError) as exc:
            run(ImageOps(tight).compress(png(1200, 1200)))
        assert exc.value.code == "image_too_large"


def test_compress_does_not_block_the_event_loop():
    """Same hazard as resize: the ladders are CPU, so they run in a thread."""

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
        # A target it cannot reach, so every rung of both ladders is walked.
        await ops.compress(noisy_png(600, 600), max_bytes=500)
        beat.cancel()
        return ticks

    assert asyncio.run(scenario()) > 0
