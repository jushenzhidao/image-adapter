"""tencent_hunyuan/images@v1: the chat-shaped Hunyuan 3.5 script, specified.

The suite is organised by the three decisions the script makes:

  * the request is flattened into ONE `user` message whose image parts precede
    the text part, with the vendor's free-form `size` and opt-in vendor fields
    forwarded and everything the upstream has no field for dropped;
  * the reference wire form is "as sent" -- URLs and data URIs ride through,
    bare base64 gains the `data:` prefix the vendor's reference names;
  * the reply side maps the final frame's `delta.image` back into the canonical
    `data` envelope, honouring `response_format` by conversion and refusing a
    picture-less 200 loudly.

No live upstream is contacted anywhere in this file: every network-shaped ctx
method is replaced with a recorder.
"""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "tencent_hunyuan"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hy = _load("tencent_hunyuan_images_v1", "images@v1.py")

UPSTREAM_URL = (
    "https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v35-generation"
)
REF_URL = "https://cdn.example/dog.png"
OTHER_URL = "https://cdn.example/cat.png"
REHOSTED = "https://our-storage.example/temp.png"
COS_URL = "https://aigc-output-image-file-1.cos.ap-guangzhou.myqcloud.com/x.png"


def _real_png(width: int = 4, height: int = 3) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


REAL_PNG = _real_png()
REAL_BARE = base64.b64encode(REAL_PNG).decode()
REAL_DATA_URI = f"data:image/png;base64,{REAL_BARE}"


def _frame(url: str = COS_URL, **extra) -> dict:
    """A successful final frame, as the vendor's reference describes it."""
    image = {"url": url, "width": 1024, "height": 1024, "source": "generate"}
    frame = {
        "id": "trace-1",
        "object": "image.chat.completion.chunk",
        "created": 1727654321,
        "model": "HY-Image-3.5-preview-4090-Tob-vX.Y",
        "choices": [{"delta": {"type": "image", "image": image}, "finish_reason": None}],
        "usage": {"total_tokens": 1200},
        "request_id": "req-vendor-1",
    }
    frame.update(extra)
    return frame


class _ChannelStub:
    upstream_key = "tokenhub-key"
    upstream_url = UPSTREAM_URL
    stage_urls: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


class Spy:
    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.uploads: list[bytes] = []
        self.rehosts: list[str] = []

    async def download(self, url: str) -> bytes:
        self.downloads.append(url)
        return REAL_PNG

    async def rehost(self, url: str):
        self.rehosts.append(url)
        return REHOSTED


def _ctx(monkeypatch, *, mapped_model: str | None = None, **options):
    ctx = AdapterContext(
        request_id="req-hy-1",
        channel=_ChannelStub(options),
        settings=Settings(minio_endpoint=""),
        mapped_model=mapped_model,
    )
    spy = Spy()
    monkeypatch.setattr(ctx, "download_image", spy.download)
    monkeypatch.setattr(ctx, "rehost_image", spy.rehost)
    return ctx, spy


async def _request(ctx, payload: dict):
    return await hy.transform(ctx, payload, "request")


async def _respond(ctx, payload: dict):
    return await hy.transform(ctx, payload, "response")


class TestRequestShape:
    """One user message; image parts before text; vendor fields only when given."""

    async def test_text_to_image_flattens_into_one_user_message(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "画一只猫", "model": "hy-image-v3.5-preview"})
        assert body["model"] == "hy-image-v3.5-preview"
        assert body["messages"] == [
            {
                "role": "user",
                "content": [{"type": "text", "text": "画一只猫"}],
            }
        ]
        # Nothing else: no size, no vendor fields, no watermark switch.
        assert set(body) == {"model", "messages"}

    async def test_a_reference_goes_before_the_text_part(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "参考这张图的风格，画一只猫", "image": REF_URL})
        content = body["messages"][0]["content"]
        assert content == [
            {"type": "image_url", "image_url": {"url": REF_URL}},
            {"type": "text", "text": "参考这张图的风格，画一只猫"},
        ]
        # The default wire form touches no network at all.
        assert spy.downloads == []
        assert spy.uploads == []

    async def test_every_reference_in_a_list_keeps_its_order(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "p", "image": [REF_URL, OTHER_URL]})
        content = body["messages"][0]["content"]
        assert [part["image_url"]["url"] for part in content[:2]] == [REF_URL, OTHER_URL]

    async def test_a_bare_base64_reference_gains_the_data_uri_prefix(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "p", "image": REAL_BARE})
        (part,) = body["messages"][0]["content"][:1]
        assert part["image_url"]["url"] == REAL_DATA_URI

    async def test_a_value_that_is_none_of_the_three_shapes_rides_through(
        self, monkeypatch
    ):
        """Neither URL nor data URI nor decodable base64: the vendor's verdict."""
        ctx, _ = _ctx(monkeypatch)
        junk = "https://[not a url"
        body = await _request(ctx, {"prompt": "p", "image": junk})
        (part,) = body["messages"][0]["content"][:1]
        assert part["image_url"]["url"] == junk

    async def test_size_is_forwarded_in_the_same_spelling(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "p", "size": "1024x1024"})
        assert body["size"] == "1024x1024"

    async def test_a_blank_or_non_string_size_is_not_sent(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        assert "size" not in await _request(ctx, {"prompt": "p", "size": "  "})
        assert "size" not in await _request(ctx, {"prompt": "p", "size": 1024})

    async def test_vendor_fields_forward_when_the_caller_sends_them(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(
            ctx,
            {
                "prompt": "p",
                "seed": 42,
                "session": "demo-session-001",
                "footnote": "ACME",
                "generate_max_pixels": 1048576,
                "resize_max_pixels": 1048576,
                "use_search_tool": {"value": True},
            },
        )
        assert body["seed"] == 42
        assert body["session"] == "demo-session-001"
        assert body["footnote"] == "ACME"
        assert body["generate_max_pixels"] == 1048576
        assert body["resize_max_pixels"] == 1048576
        assert body["use_search_tool"] == {"value": True}

    async def test_vendor_fields_default_to_absent_not_off(self, monkeypatch):
        """No caller value, no option: nothing is invented, watermark included."""
        ctx, _ = _ctx(monkeypatch, footnote="CHANNEL")
        body = await _request(ctx, {"prompt": "p"})
        assert body["footnote"] == "CHANNEL"  # option fills the gap
        assert "seed" not in body
        assert "session" not in body
        assert "watermark" not in body

    async def test_fields_without_an_upstream_meaning_are_dropped(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(
            ctx, {"prompt": "p", "n": 3, "quality": "high", "style": "vivid"}
        )
        for absent in ("n", "quality", "style", "mask", "watermark"):
            assert absent not in body

    async def test_the_callers_model_label_is_not_forwarded_verbatim(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "p", "model": "gpt-image-2"})
        assert body["model"] == "hy-image-v3.5-preview"

    async def test_a_channel_option_model_wins_over_the_default(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, model="hy-image-v3.0")
        body = await _request(ctx, {"prompt": "p"})
        assert body["model"] == "hy-image-v3.0"

    async def test_a_model_map_hit_wins_over_everything(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, mapped_model="hy-image-v3.5-preview", model="other")
        body = await _request(ctx, {"prompt": "p"})
        assert body["model"] == "hy-image-v3.5-preview"

    async def test_an_image_without_a_prompt_is_a_valid_request(self, monkeypatch):
        """The front door allows a bare image (restyle); the script keeps it."""
        ctx, _ = _ctx(monkeypatch)
        body = await _request(ctx, {"prompt": "", "image": REF_URL})
        content = body["messages"][0]["content"]
        assert content == [{"type": "image_url", "image_url": {"url": REF_URL}}]


class TestResponseShape:
    """The final frame's `delta.image` becomes the canonical data envelope."""

    async def test_a_url_answer_is_mapped_with_its_extras(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch)
        out = await _respond(ctx, _frame())
        assert out["created"] == 1727654321
        (item,) = out["data"]
        assert item == {"url": COS_URL, "width": 1024, "height": 1024}
        assert out["usage"] == {"total_tokens": 1200}
        assert spy.downloads == []  # `url` asked, `url` given: no fetch

    async def test_b64_json_is_honoured_by_downloading_the_frame_link(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch)
        await _request(ctx, {"prompt": "p", "response_format": "b64_json"})
        out = await _respond(ctx, _frame())
        (item,) = out["data"]
        assert "url" not in item
        assert base64.b64decode(item["b64_json"]) == REAL_PNG
        assert spy.downloads == [COS_URL]

    async def test_an_unspecified_format_passes_the_link_through(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch)
        out = await _respond(ctx, _frame())
        assert out["data"][0]["url"] == COS_URL
        assert spy.rehosts == []

    async def test_rehost_url_swaps_the_twelve_hour_link_for_ours(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, rehost_url=True)
        out = await _respond(ctx, _frame())
        assert out["data"][0]["url"] == REHOSTED
        assert spy.rehosts == [COS_URL]

    async def test_rehost_url_stays_off_for_b64_json(self, monkeypatch):
        """The picture is already inlined: re-hosting it would fetch twice."""
        ctx, spy = _ctx(monkeypatch, rehost_url=True)
        await _request(ctx, {"prompt": "p", "response_format": "b64_json"})
        out = await _respond(ctx, _frame())
        assert base64.b64decode(out["data"][0]["b64_json"]) == REAL_PNG
        assert spy.rehosts == []

    async def test_a_failed_frame_is_a_loud_502_not_a_quiet_200(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        with pytest.raises(AdapterError) as ei:
            await _respond(ctx, _frame(error={"code": "internal", "message": "boom"}))
        assert ei.value.status == 502
        assert ei.value.code == "upstream_error"
        assert "boom" in ei.value.message

    async def test_a_200_without_a_picture_fails_loudly(self, monkeypatch):
        """The reference: judge success by `image.url`, not `finish_reason`."""
        ctx, _ = _ctx(monkeypatch)
        frame = _frame()
        frame["choices"][0]["delta"] = {"type": "text", "text": "done"}
        with pytest.raises(AdapterError) as ei:
            await _respond(ctx, frame)
        assert ei.value.status == 502
        assert ei.value.code == "upstream_error"

    async def test_a_refusal_wording_is_a_400_content_filter(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        with pytest.raises(AdapterError) as ei:
            await _respond(
                ctx,
                _frame(error={"code": "x", "message": "内容安全警告：输入包含不适当的内容"}),
            )
        assert ei.value.status == 400
        assert ei.value.code == "content_filter"

    async def test_usage_is_dropped_when_the_vendor_sends_none(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        frame = _frame()
        frame.pop("usage")
        out = await _respond(ctx, frame)
        assert "usage" not in out


class TestStateAcrossPhases:
    """`response_format` parked per request id, consumed exactly once."""

    async def test_two_requests_do_not_swap_their_shapes(self, monkeypatch):
        first, _ = _ctx(monkeypatch)
        second, _ = _ctx(monkeypatch)
        second.request_id = "req-hy-2"
        await _request(first, {"prompt": "p", "response_format": "b64_json"})
        await _request(second, {"prompt": "p"})  # unspecified
        out_b64 = await _respond(first, _frame())
        out_url = await _respond(second, _frame())
        assert "b64_json" in out_b64["data"][0]
        assert out_url["data"][0]["url"] == COS_URL

    async def test_the_parked_shape_is_consumed_by_one_response_only(
        self, monkeypatch
    ):
        ctx, _ = _ctx(monkeypatch)
        await _request(ctx, {"prompt": "p", "response_format": "b64_json"})
        await _respond(ctx, _frame())
        # A second frame on the same ctx falls back to the pass-through shape
        # rather than re-reading a stale instruction.
        out = await _respond(ctx, _frame())
        assert out["data"][0]["url"] == COS_URL
