"""tencent_ci/images@v1: the 数据万象 (super-res + matting) script, specified.

The suite is organised by the decisions the script makes:

  * the operation resolves mapped model > channel option > a raw model that
    names an op verbatim, and refuses loudly when none does -- no silent
    default, because a wrong op is a different billable API;
  * the request is one signed GET: the wire query and the COS signature are
    built from the same encoded pairs, every reference is probed (bytes ->
    dimensions/format) so nonsense fails HERE with real numbers instead of
    the vendor's misleading ImageTooLarge, oversized inputs are downscaled
    to fit automatically, and `size` names the desired OUTPUT edge from
    which magnify is derived;
  * the reply side turns the returned image bytes into the canonical `data`
    envelope, honouring `response_format` by conversion and refusing a
    picture-less 200 loudly;
  * `response_format` is parked per request id and consumed exactly once.

No live upstream is contacted anywhere in this file: the network-shaped ctx
methods (object-storage upload, image download) are recorders, and the COS
signature is pinned against an independent restatement of the vendor SDK's
algorithm at a frozen clock. That proves the script computes what the SDK
computes, not that COS accepts it -- the live proof happened once, against
the real bucket, on 2026-10-08 (see MEMORY-ops §K); this suite keeps it
honest without ever touching the billable path.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import urllib.parse
from pathlib import Path

import pytest
from PIL import Image

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "tencent_ci"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci = _load("tencent_ci_images_v1", "images@v1.py")

BUCKET_URL = "https://demo-bucket-1250000000.cos.ap-guangzhou.myqcloud.com/"
SECRET_ID = "AKIDzRrLxJpTestExample"
SECRET_KEY = "cFxcGuSp2LcuEVvaCgz8B7ucFsZO7gEk"
REF_URL = "https://cdn.example/dog.png"
REHOSTED = "https://our-storage.example/temp.png"
DATA_URI_TOLD = "data:image/png;base64,AAAA"


def _png(width: int = 64, height: int = 64, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format=fmt)
    return buf.getvalue()


def _data_uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode()


REAL_PNG = _png()  # 64x64: past the 32px floor the vendor enforces
REAL_BARE = base64.b64encode(REAL_PNG).decode()
REAL_DATA_URI = _data_uri(REAL_PNG)


class _ChannelStub:
    upstream_key = f"{SECRET_ID}|{SECRET_KEY}"
    upstream_url = BUCKET_URL
    stage_urls: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


class Spy:
    """Stands in for object storage and the image downloader; records all."""

    def __init__(self, answer: str = REHOSTED, payload: bytes | None = None) -> None:
        self.answer = answer
        self.payload = payload if payload is not None else REAL_PNG
        self.uploads: list[bytes] = []
        self.downloads: list[str] = []

    async def upload(self, data: bytes, ext: str = "png") -> str:
        self.uploads.append(data)
        return self.answer

    async def download(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.payload


def _ctx(monkeypatch, *, mapped_model: str | None = None, payload: bytes | None = None, **options):
    ctx = AdapterContext(
        request_id="req-ci-1",
        channel=_ChannelStub(options),
        settings=Settings(minio_endpoint=""),
        mapped_model=mapped_model,
    )
    spy = Spy(payload=payload)
    monkeypatch.setattr(ctx, "upload_temp_image", spy.upload)
    monkeypatch.setattr(ctx, "download_image", spy.download)
    return ctx, spy


async def _request(ctx, payload: dict):
    return await ci.transform(ctx, payload, "request")


async def _respond(ctx, payload):
    return await ci.transform(ctx, payload, "response")


def _plan_query(ctx) -> dict[str, str]:
    """The emitted URL's query, decoded back into a dict."""
    query = urllib.parse.urlparse(ctx.plan.url).query
    return {
        k: v
        for k, v in (pair.split("=", 1) for pair in query.split("&"))
    }


def _fake_info(monkeypatch, ctx, **facts) -> None:
    """Replaces Pillow's read of the image with a fabricated verdict."""

    async def fake_info(data: bytes):
        base = {
            "width": 64, "height": 64, "format": "PNG",
            "mode": "RGB", "bytes": len(data),
        }
        base.update(facts)
        return base

    monkeypatch.setattr(ctx.image, "info", fake_info)


def _expected_authorization(
    key_time: int,
    pathname: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> str:
    """The vendor SDK's algorithm, restated independently (a drift alarm)."""

    def enc(value) -> str:
        return urllib.parse.quote(str(value), safe="-_.~")

    encoded_params = {enc(k).lower(): enc(v) for k, v in params.items()}
    encoded_headers = {enc(k).lower(): enc(v) for k, v in headers.items()}
    param_str = "&".join(f"{k}={v}" for k, v in sorted(encoded_params.items()))
    header_str = "&".join(f"{k}={v}" for k, v in sorted(encoded_headers.items()))
    http_string = f"get\n{pathname}\n{param_str}\n{header_str}\n"
    sign_key = hmac.new(
        SECRET_KEY.encode(), str(key_time).encode(), hashlib.sha1
    ).hexdigest()
    string_to_sign = (
        f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    )
    signature = hmac.new(
        sign_key.encode(), string_to_sign.encode(), hashlib.sha1
    ).hexdigest()
    return (
        f"q-sign-algorithm=sha1&q-ak={SECRET_ID}"
        f"&q-sign-time={key_time}&q-key-time={key_time}"
        f"&q-header-list={';'.join(sorted(encoded_headers))}"
        f"&q-url-param-list={';'.join(sorted(encoded_params))}"
        f"&q-signature={signature}"
    )


FROZEN_NOW = 1_760_000_000


class TestOperationResolution:
    """mapped model > option > a raw model that names an op > loud refusal."""

    async def test_a_mapped_model_names_the_operation(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["ci-process"] == "GoodsMatting"

    async def test_an_option_pins_the_operation_when_nothing_is_mapped(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch, op="AIPortraitMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["ci-process"] == "AIPortraitMatting"

    async def test_a_raw_model_that_names_an_op_is_accepted_verbatim(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        await _request(ctx, {"prompt": "", "image": REF_URL, "model": "GoodsMatting"})
        assert _plan_query(ctx)["ci-process"] == "GoodsMatting"

    async def test_an_arbitrary_raw_model_is_not_a_guessable_op(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        with pytest.raises(AdapterError) as ei:
            await _request(
                ctx, {"prompt": "", "image": REF_URL, "model": "gpt-image-2"}
            )
        assert ei.value.code == "channel_config_error"

    async def test_a_gateway_model_label_does_not_leak_into_ci_process(
        self, monkeypatch
    ):
        """A mapped value outside the op set is ignored, not forwarded."""
        ctx, _ = _ctx(monkeypatch, mapped_model="gpt-image-2", op="GoodsMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["ci-process"] == "GoodsMatting"


class TestRequestShape:
    """One signed GET; URL through, inline re-hosted; op-specific params only."""

    async def test_a_url_reference_is_probed_then_rides_through(self, monkeypatch):
        """预检会下载一份探测字节，但 detect-url 仍传原 URL（上游自己拉）。"""
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        body = await _request(ctx, {"prompt": "", "image": REF_URL})
        assert body == {}
        assert ctx.plan.method == "GET"
        query = _plan_query(ctx)
        assert query["ci-process"] == "AISuperResolution"
        assert urllib.parse.unquote(query["detect-url"]) == REF_URL
        assert query["magnify"] == "2"  # the documented default
        assert ctx.plan.url.startswith(BUCKET_URL.rstrip("/") + "/?")
        assert spy.downloads == [REF_URL]  # the probe copy, for the preflight
        assert spy.uploads == []  # 未超限：URL 原样直通，不转存

    async def test_an_inline_reference_is_rehosted_first(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REAL_DATA_URI})
        query = _plan_query(ctx)
        assert urllib.parse.unquote(query["detect-url"]) == REHOSTED
        assert spy.uploads == [REAL_PNG]

    async def test_bare_base64_is_rehosted_too(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REAL_BARE})
        assert spy.uploads == [REAL_PNG]

    async def test_no_storage_means_a_loud_503_not_a_data_uri(self, monkeypatch):
        """A data URI in detect-url's slot would 400 at COS, unfetchable."""
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        degraded = Spy(DATA_URI_TOLD)
        monkeypatch.setattr(ctx, "upload_temp_image", degraded.upload)
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": REAL_DATA_URI})
        assert ei.value.status == 503
        assert ei.value.code == "storage_unavailable"
        assert degraded.uploads == [REAL_PNG]  # the upload ran, its answer was unusable

    async def test_no_image_is_a_400(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "放大"})
        assert ei.value.status == 400
        assert ei.value.param == "image"

    async def test_two_images_are_refused(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": [REF_URL, REF_URL]})
        assert ei.value.status == 400
        assert ei.value.param == "image"

    async def test_a_bad_key_format_is_a_channel_config_error(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        monkeypatch.setattr(ctx, "key", "just-a-bearer-token", raising=False)
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": REF_URL})
        assert ei.value.code == "channel_config_error"


class TestPreflight:
    """修不了的在这里拒（带真实数字）；修的了的交给自动缩图。"""

    async def test_an_input_under_the_32px_floor_is_refused(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        small = _png(16, 16)
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": _data_uri(small)})
        assert ei.value.status == 400
        assert ei.value.param == "image"
        assert "16×16" in ei.value.message

    async def test_a_non_png_jpeg_format_is_refused(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        gif = _png(64, 64, fmt="GIF")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": _data_uri(gif)})
        assert ei.value.param == "image"
        assert "PNG/JPEG" in ei.value.message

    async def test_a_portrait_8k_matting_is_not_misjudged(self, monkeypatch):
        """4320×7680 竖版按字面读会撞「高 ≤4320」——这条边故意不预检。"""
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        _fake_info(monkeypatch, ctx, width=4320, height=7680)
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert urllib.parse.unquote(_plan_query(ctx)["detect-url"]) == REF_URL

    async def test_a_matting_past_the_long_edge_switches_to_stored(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch, mapped_model="GoodsMatting")
        _fake_info(monkeypatch, ctx, width=8192, height=8192)

        async def noop_resize(data, w, h, keep_ratio=True, fmt=None):
            return data

        monkeypatch.setattr(ctx.image, "resize", noop_resize)
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert urllib.parse.unquote(_plan_query(ctx)["detect-url"]) == REHOSTED


class TestAutoDownscale:
    """超上游上限的输入自动缩到 ≤ 上限；调用方无需预处理。"""

    async def test_an_oversized_inline_input_is_downscaled(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        big = _png(2048, 2048)
        await _request(ctx, {"prompt": "", "image": _data_uri(big), "magnify": 1})
        query = _plan_query(ctx)
        assert urllib.parse.unquote(query["detect-url"]) == REHOSTED  # 缩过 ⇒ 必转存
        stored = Image.open(io.BytesIO(spy.uploads[0]))
        assert stored.size == (1920, 1920)

    async def test_an_oversized_url_input_switches_to_stored_link(
        self, monkeypatch
    ):
        ctx, spy = _ctx(
            monkeypatch, mapped_model="AISuperResolution", payload=_png(2048, 2048)
        )
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert spy.downloads == [REF_URL]  # probe copy
        stored = Image.open(io.BytesIO(spy.uploads[0]))
        assert stored.size == (1920, 1920)
        assert urllib.parse.unquote(_plan_query(ctx)["detect-url"]) == REHOSTED  # 原链接不再直通

    async def test_a_compliant_input_is_never_resized(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REAL_DATA_URI})
        assert spy.uploads == [REAL_PNG]  # 原字节原样转存


class TestMagnify:
    """显式 body > size 推导 > 选项 > 默认 2；仅超分有该参数。"""

    async def test_the_body_wins_over_the_option(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution", magnify=2)
        await _request(ctx, {"prompt": "", "image": REF_URL, "magnify": 4})
        assert _plan_query(ctx)["magnify"] == "4"

    async def test_the_option_fills_the_gap(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution", magnify=4)
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["magnify"] == "4"

    async def test_a_digit_string_option_is_repaired(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution", magnify="4")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["magnify"] == "4"

    @pytest.mark.parametrize("bad", [3, True, "3x", 1.5])
    async def test_nonsense_magnify_is_a_400(self, monkeypatch, bad):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": REF_URL, "magnify": bad})
        assert ei.value.status == 400
        assert ei.value.param == "magnify"

    async def test_a_matting_op_has_no_magnify(self, monkeypatch):
        """The upstream has no such field on mattings -- dropped, not sent."""
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL, "magnify": 4})
        assert "magnify" not in _plan_query(ctx)


class TestSize:
    """`size` = 期望输出边长：magnify 由它推导；matting 不认识它。"""

    async def test_2k_on_a_small_input_picks_magnify_4(self, monkeypatch):
        """64 输入问 2K：四个可达档里 64×4=256 离 2048 最近。"""
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2k"})
        assert _plan_query(ctx)["magnify"] == "4"
        assert spy.downloads == [REF_URL]  # 未超限，URL 原样直通

    async def test_the_tier_word_is_case_insensitive(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2K"})
        assert _plan_query(ctx)["magnify"] == "4"

    async def test_wxh_spelling_takes_the_long_edge(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2048x2048"})
        assert _plan_query(ctx)["magnify"] == "4"

    async def test_2k_on_a_1024_input_picks_magnify_2(self, monkeypatch):
        ctx, _ = _ctx(
            monkeypatch, mapped_model="AISuperResolution", payload=_png(1024)
        )
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2k"})
        assert _plan_query(ctx)["magnify"] == "2"  # 1024×2 = 2048, exact

    async def test_an_input_at_the_target_gets_clarity_only(self, monkeypatch):
        """1920 输入问 2K：m=1 差 128，m=2 超 1792 —— 就近取小，只做增强。"""
        ctx, _ = _ctx(
            monkeypatch, mapped_model="AISuperResolution", payload=_png(1920)
        )
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2k"})
        assert _plan_query(ctx)["magnify"] == "1"

    async def test_an_explicit_magnify_beats_the_size_derivation(self, monkeypatch):
        ctx, _ = _ctx(
            monkeypatch, mapped_model="AISuperResolution", payload=_png(1024)
        )
        await _request(
            ctx, {"prompt": "", "image": REF_URL, "size": "2k", "magnify": 4}
        )
        assert _plan_query(ctx)["magnify"] == "4"

    async def test_a_channel_option_size_works_too(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution", size="4k")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["magnify"] == "4"  # 64×4=256, closest to 4096

    async def test_an_input_already_at_the_target_short_circuits(self, monkeypatch):
        """2048×1152 输入问 2k：原图直出——不发上游、零计费、不缩图。"""
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        big = _png(2048, 1152)
        await _request(ctx, {"prompt": "", "image": _data_uri(big), "size": "2k"})
        assert ctx.plan.local_result == big  # 引擎将把这份字节当上游产物
        assert ctx.plan.url is None  # 没有要发的调用
        assert spy.uploads == [] and spy.downloads == []

    async def test_an_explicit_magnify_disables_the_shortcut(self, monkeypatch):
        """显式 magnify=2 是「真跑一次增强」：超限照旧缩图送上游。"""
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(
            ctx, {"prompt": "", "image": _data_uri(_png(2048, 1152)), "magnify": 2}
        )
        assert ctx.plan.local_result is None
        stored = Image.open(io.BytesIO(spy.uploads[0]))
        assert stored.size == (1920, 1080)

    async def test_below_the_target_still_runs_the_upstream(self, monkeypatch):
        ctx, _ = _ctx(
            monkeypatch, mapped_model="AISuperResolution", payload=_png(1024)
        )
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2k"})
        assert ctx.plan.local_result is None  # 1024 < 2048：正常送上游
        assert _plan_query(ctx)["magnify"] == "2"

    async def test_a_nonsense_size_is_a_400(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": REF_URL, "size": "abc"})
        assert ei.value.param == "size"

    async def test_size_is_meaningless_on_matting(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL, "size": "2k"})
        query = _plan_query(ctx)
        assert "magnify" not in query
        assert "size" not in query


class TestMattingLayout:
    """center-layout and padding-layout: matting-only, validated, not rewritten."""

    async def test_center_layout_defaults_to_absent_zero(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AIPicMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert _plan_query(ctx)["center-layout"] == "0"

    async def test_center_layout_body_wins_and_is_sent_canonically(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AIPicMatting", **{"center-layout": 1})
        await _request(ctx, {"prompt": "", "image": REF_URL, "center-layout": "1"})
        assert _plan_query(ctx)["center-layout"] == "1"

    async def test_center_layout_beyond_the_enum_is_a_400(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AIPicMatting")
        with pytest.raises(AdapterError) as ei:
            await _request(ctx, {"prompt": "", "image": REF_URL, "center-layout": 5})
        assert ei.value.param == "center-layout"

    async def test_padding_layout_is_forwarded_after_validation(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        await _request(
            ctx, {"prompt": "", "image": REF_URL, "padding-layout": "20 x 10"}
        )
        assert _plan_query(ctx)["padding-layout"] == "20x10"

    async def test_padding_layout_absent_is_not_sent(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        assert "padding-layout" not in _plan_query(ctx)

    @pytest.mark.parametrize("bad", ["1001x10", "10", "axb", "-10x10"])
    async def test_padding_layout_nonsense_is_a_400(self, monkeypatch, bad):
        ctx, _ = _ctx(monkeypatch, mapped_model="GoodsMatting")
        with pytest.raises(AdapterError) as ei:
            await _request(
                ctx, {"prompt": "", "image": REF_URL, "padding-layout": bad}
            )
        assert ei.value.param == "padding-layout"

    async def test_the_super_res_op_has_no_layout_fields(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(
            ctx,
            {"prompt": "", "image": REF_URL, "center-layout": 1,
             "padding-layout": "20x10"},
        )
        query = _plan_query(ctx)
        assert "center-layout" not in query
        assert "padding-layout" not in query


class TestSignature:
    """The emitted Authorization is the vendor SDK's algorithm, byte for byte."""

    async def test_the_authorization_matches_the_sdk_algorithm(self, monkeypatch):
        monkeypatch.setattr(ci.time, "time", lambda: FROZEN_NOW)
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL})

        params = {
            "ci-process": "AISuperResolution",
            "detect-url": REF_URL,
            "magnify": "2",
        }
        expected = _expected_authorization(
            f"{FROZEN_NOW - 60};{FROZEN_NOW + 900}", "/", params,
            {"host": "demo-bucket-1250000000.cos.ap-guangzhou.myqcloud.com"},
        )
        assert ctx.plan.headers["Authorization"] == expected

    async def test_only_the_host_is_signed_but_the_query_is_listed(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        auth = ctx.plan.headers["Authorization"]
        assert "q-header-list=host" in auth
        listed = auth.split("q-url-param-list=")[1].split("&")[0]
        assert set(listed.split(";")) == {"ci-process", "detect-url", "magnify"}

    async def test_the_wire_query_carries_exactly_what_was_signed(self, monkeypatch):
        """Signature and URL are pinned to the same encoded pairs."""
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(ctx, {"prompt": "", "image": REF_URL})
        params = {
            "ci-process": "AISuperResolution",
            "detect-url": REF_URL,
            "magnify": "2",
        }
        expected_query = "&".join(
            f"{urllib.parse.quote(k, safe='-_.~').lower()}="
            f"{urllib.parse.quote(v, safe='-_.~')}"
            for k, v in sorted(params.items())
        )
        assert urllib.parse.urlparse(ctx.plan.url).query == expected_query
        # and the Host header matches the bucket, so what was signed is sent
        assert ctx.plan.headers["Host"] == (
            "demo-bucket-1250000000.cos.ap-guangzhou.myqcloud.com"
        )


class TestResponseShape:
    """Image bytes in -> one canonical item out; a picture-less 200 is loud."""

    async def test_the_default_answer_is_a_url_of_ours(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch)
        out = await _respond(ctx, REAL_PNG)
        (item,) = out["data"]
        assert item["url"] == REHOSTED
        assert spy.uploads == [REAL_PNG]
        assert item["width"] == 64 and item["height"] == 64

    async def test_b64_json_is_honoured_without_an_upload(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(
            ctx, {"prompt": "", "image": REF_URL, "response_format": "b64_json"}
        )
        out = await _respond(ctx, REAL_PNG)
        (item,) = out["data"]
        assert "url" not in item
        assert base64.b64decode(item["b64_json"]) == REAL_PNG
        assert spy.uploads == []

    async def test_a_degraded_store_answers_b64_not_a_fake_url(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        monkeypatch.setattr(ctx, "upload_temp_image", Spy(DATA_URI_TOLD).upload)
        out = await _respond(ctx, REAL_PNG)
        (item,) = out["data"]
        assert "url" not in item
        assert base64.b64decode(item["b64_json"]) == REAL_PNG

    async def test_a_200_without_image_magic_fails_loudly(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        with pytest.raises(AdapterError) as ei:
            await _respond(ctx, b"<html>not an image</html>")
        assert ei.value.status == 502
        assert ei.value.code == "upstream_error"

    async def test_a_json_payload_is_not_bytes_and_fails_loudly(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        with pytest.raises(AdapterError) as ei:
            await _respond(ctx, {"unexpected": "frame"})
        assert ei.value.status == 502
        assert ei.value.code == "upstream_error"


class TestStateAcrossPhases:
    """`response_format` parked per request id, consumed exactly once."""

    async def test_two_requests_do_not_swap_their_shapes(self, monkeypatch):
        first, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        second, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        second.request_id = "req-ci-2"
        await _request(
            first, {"prompt": "", "image": REF_URL, "response_format": "b64_json"}
        )
        await _request(second, {"prompt": "", "image": REF_URL})  # unspecified
        out_b64 = await _respond(first, REAL_PNG)
        out_url = await _respond(second, REAL_PNG)
        assert "b64_json" in out_b64["data"][0]
        assert out_url["data"][0]["url"] == REHOSTED

    async def test_the_parked_shape_is_consumed_by_one_response_only(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch, mapped_model="AISuperResolution")
        await _request(
            ctx, {"prompt": "", "image": REF_URL, "response_format": "b64_json"}
        )
        await _respond(ctx, REAL_PNG)
        # A second frame on the same ctx falls back to the default shape
        # rather than re-reading a stale instruction.
        out = await _respond(ctx, REAL_PNG)
        assert out["data"][0]["url"] == REHOSTED
