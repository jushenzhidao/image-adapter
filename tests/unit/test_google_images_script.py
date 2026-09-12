"""Unit tests for the pure logic inside google/images@v1.

The end-to-end suite proves the wire shape; this file pins the mapping rules that
break silently -- ratio and tier clamping, alias resolution, mime inference, usage
mapping -- without paying for an HTTP server per case.

The script is loaded by path rather than by ref: a unit test should fail on the
function it is about, not on script-store resolution.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from adapter.ctxapi.fanout import FanoutMixin
from adapter.ctxapi.mapping import MappingMixin
from adapter.sandbox import scan_source
from adapter.settings import Settings

SCRIPT = (
    Path(__file__).resolve().parents[2] / "script_store" / "google" / "images@v1.py"
)
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("google_images_v1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


g = _load()


class FakeCtx(MappingMixin, FanoutMixin):
    """Just enough ctx for the helpers that take one.

    The mapping helpers are inherited from the real mixin -- they are pure and the
    script's behaviour depends on them, so stubbing them would test the stub. Only
    the infrastructure (caps, emit, fail) is faked. The fan-out mixin is inherited
    for the same reason, and because the script reaches for `ctx.fanout` on *every*
    request that carries references -- the zero-reference case included, where the
    call returns without doing anything. A stub without it fails on the attribute
    rather than on anything this file is about.
    """

    def __init__(self, caps=None, **options):
        self.options = options
        self.settings = Settings(minio_endpoint="", fanout_concurrency=4)
        self.request_id = "req-1"
        self.upstream_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-image:generateContent"
        )
        self.failed = None
        self.emitted_url = None
        self._caps = caps

    def caps(self, vendor, model=None):
        return self._caps

    def emit(self, *, url=None, **kw):
        if url is not None:
            self.emitted_url = url

    def fail(self, message, **kw):
        self.failed = (message, kw)
        raise AssertionError(message)


#: Capability facts are data now (test_capabilities.py covers the loader, and the
#: arithmetic lives in test_mapping.py), so these tests feed the shapes directly and
#: stay about what the script ends up putting on the wire.
RATIOS = ["1:1", "1:4", "1:8", "4:3", "3:4", "16:9", "9:16", "21:9"]

FIXED = {"tiers": [], "wide": False, "ratios": RATIOS}   # model picks its own resolution
FLASH = {"tiers": ["512", "1K", "2K", "4K"], "wide": True, "ratios": RATIOS}
PRO = {"tiers": ["1K", "2K", "4K"], "wide": True, "ratios": RATIOS}


def test_the_shipped_script_passes_the_sandbox_scanner():
    """A script that cannot clear the AST policy never runs at all."""
    assert scan_source(SOURCE) is not None


# --- model resolution -------------------------------------------------------


def test_the_model_is_written_into_the_path():
    url = "https://h/v1beta/models/gemini-2.5-flash-image:generateContent?key=k"
    assert g._model_url(url, "gemini-3-pro-image") == (
        "https://h/v1beta/models/gemini-3-pro-image:generateContent?key=k"
    )
    # Not a generateContent URL: leave it alone rather than mangling it.
    assert g._model_url("https://h/v1/images/generations", "x") == (
        "https://h/v1/images/generations"
    )


# --- size mapping at the request phase (the arithmetic itself is in test_mapping) --


@pytest.mark.parametrize(
    "ratios, wide, size, expected_aspect, expected_tier",
    [
        (RATIOS, True, "1920x1080", "16:9", "2K"),
        (RATIOS, True, "1024x1024", "1:1", "1K"),
        # A folded shape (1:4) on a model that refuses them falls back to the
        # nearest remaining one; the long edge still asks for the 4K tier.
        (RATIOS, False, "1024x4096", "9:16", "4K"),
        # Below every tier: the floor, never the ceiling (see test_mapping).
        (RATIOS, True, "300x300", "1:1", "512"),
    ],
)
def test_the_wire_carries_the_mapped_shape(
    ratios, wide, size, expected_aspect, expected_tier
):
    caps = {"model": "gemini-3.1-flash-image", "tiers": FLASH["tiers"],
            "wide": wide, "ratios": ratios}
    _, body = asyncio.run(_request_phase(caps, size=size))
    inner = body["generationConfig"]["responseFormat"]["image"]
    assert inner["aspectRatio"] == expected_aspect
    assert inner["imageSize"] == expected_tier


def test_a_model_without_tiers_gets_a_ratio_but_no_image_size():
    _, body = asyncio.run(
        _request_phase({"model": "gemini-2.5-flash-image", "tiers": [],
                        "wide": False, "ratios": RATIOS},
                       model="gemini-2.5-flash-image", size="1920x1080")
    )
    inner = body["generationConfig"]["imageConfig"]
    assert inner == {"aspectRatio": "16:9"}
    assert "imageSize" not in inner


def test_an_unparsable_size_falls_back_to_square_1k():
    _, body = asyncio.run(_request_phase({"model": "gemini-3.1-flash-image", **FLASH},
                                         size="not-a-size"))
    inner = body["generationConfig"]["responseFormat"]["image"]
    assert inner == {"aspectRatio": "1:1", "imageSize": "1K"}


async def _request_phase(caps, model="nano-banana-pro", size="1024x1024", **options):
    """Runs the shipped request phase with injected facts. Returns (ctx, body)."""
    ctx = FakeCtx(caps, **options)
    payload = {"model": model, "prompt": "a fox", "size": size}
    return ctx, await g.transform(ctx, payload, "request")


def test_the_request_phase_uses_the_resolved_model_and_its_tiers():
    ctx, body = asyncio.run(_request_phase({"model": "gemini-3.1-flash-image", **FLASH}))
    assert ctx.emitted_url.endswith("/models/gemini-3.1-flash-image:generateContent")
    # 3.1-generation ids take the newer field shape; 1024x1024 maps to 1:1 + 1K.
    assert body["generationConfig"]["responseFormat"]["image"] == {
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }


def test_an_older_generation_gets_the_legacy_field_shape():
    ctx, body = asyncio.run(_request_phase({"model": "gemini-3-pro-image", **PRO}))
    assert ctx.emitted_url.endswith("/models/gemini-3-pro-image:generateContent")
    assert body["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }


def test_without_facts_the_script_sends_no_image_config():
    """No table means "we know nothing": send less, do not guess a resolution."""
    ctx, body = asyncio.run(_request_phase(None, model="gemini-3.1-flash-image-preview"))
    assert ctx.emitted_url.endswith(
        "/models/gemini-3.1-flash-image-preview:generateContent"
    )
    assert "imageConfig" not in body["generationConfig"]
    assert "responseFormat" not in body["generationConfig"]
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]


def test_without_facts_an_alias_cannot_be_invented_into_a_model_id():
    """Aliases live in the table, so with no table they cannot be resolved -- and an
    id we cannot recognise is rejected rather than sent upstream to fail."""
    ctx = FakeCtx(None)
    with pytest.raises(AssertionError):
        asyncio.run(g.transform(ctx, {"model": "nano-banana-pro", "prompt": "x"}, "request"))
    assert ctx.failed[1]["param"] == "model"


def test_the_response_format_still_travels_across_phases():
    """_remember/_STATE are in-process state, not capability facts."""
    g._STATE.clear()
    ctx = FakeCtx({"model": "gemini-3-pro-image", **PRO})
    asyncio.run(g.transform(ctx, {"model": "gemini-3-pro-image", "prompt": "x",
                                  "response_format": "url"}, "request"))
    assert g._STATE["req-1"] == {"response_format": "url"}
    g._STATE.clear()


# The size/ratio/tier arithmetic used to live here as three private helpers and is
# now ctx.size_to_px / fit_ratio / fit_tier (see test_mapping.py for its own cases).
# What is asserted above is the part that is still this script's job: turning client
# intent into the exact block a Gemini endpoint wants.


# --- reference handling -----------------------------------------------------


def test_b64_size_over_estimates_rather_than_under():
    import base64

    payload = b"x" * 3000
    encoded = base64.b64encode(payload).decode()
    assert g._b64_size(encoded) >= len(payload)
    assert g._b64_size("data:image/png;base64," + encoded) >= len(payload)


def test_data_uri_mime_reads_the_prefix():
    assert g._data_uri_mime("data:image/jpeg;base64,AAAA") == "image/jpeg"
    assert g._data_uri_mime("data:;base64,AAAA") == "image/png"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://cdn.example.com/a/photo.jpeg", "image/jpeg"),
        ("https://cdn.example.com/a/photo.JPG", "image/jpeg"),
        ("https://cdn.example.com/a/photo.webp", "image/webp"),
        ("https://cdn.example.com/no-extension", "image/png"),
    ],
)
def test_declared_mime_comes_from_the_extension(url, expected):
    assert g._declared_mime(FakeCtx(), url) == expected


def test_declared_mime_honours_the_channel_default():
    assert g._declared_mime(FakeCtx(default_mime_type="image/webp"), "https://h/x") == (
        "image/webp"
    )


def test_refs_of_rejects_a_non_string_reference():
    ctx = FakeCtx()
    with pytest.raises(AssertionError):
        g._refs_of({"image": [1234]}, ctx)
    assert ctx.failed[1]["param"] == "image"


def test_refs_of_accepts_a_scalar_or_a_list():
    ctx = FakeCtx()
    assert g._refs_of({"image": "abc"}, ctx) == ["abc"]
    assert g._refs_of({"image": ["a", "b"]}, ctx) == ["a", "b"]
    assert g._refs_of({}, ctx) == []


# --- image config -----------------------------------------------------------


def test_image_config_speaks_both_generations():
    assert g._image_config("imageConfig", "16:9", "2K") == {
        "imageConfig": {"aspectRatio": "16:9", "imageSize": "2K"}
    }
    assert g._image_config("responseFormat", "1:1", None) == {
        "responseFormat": {"image": {"aspectRatio": "1:1"}}
    }


def test_image_size_is_omitted_when_the_model_fixes_it():
    assert "imageSize" not in g._image_config("imageConfig", "1:1", None)["imageConfig"]


# --- response mapping -------------------------------------------------------


def test_images_are_collected_from_both_spellings():
    camel = {"candidates": [{"content": {"parts": [
        {"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "AAA"}}]}}]}
    snake = {"candidates": [{"content": {"parts": [
        {"inline_data": {"mime_type": "image/jpeg", "data": "BBB"}}]}}]}
    assert g._collect_images(camel) == [("image/png", "AAA")]
    assert g._collect_images(snake) == [("image/jpeg", "BBB")]
    assert g._collect_images({}) == []
    assert g._collect_images({"candidates": None}) == []


def test_finish_reason_and_refusal_text():
    payload = {"candidates": [{"finishReason": "IMAGE_SAFETY",
                               "content": {"parts": [{"text": "I can't do that."}]}}]}
    assert g._finish_reason(payload) == "IMAGE_SAFETY"
    assert g._refusal_text(payload) == "I can't do that."
    assert g._finish_reason({}) == ""
    assert g._refusal_text({}) == ""


def test_usage_maps_tokens_and_adds_thoughts_to_output():
    payload = {"usageMetadata": {
        "promptTokenCount": 25,
        "candidatesTokenCount": 1120,
        "thoughtsTokenCount": 100,
        "totalTokenCount": 1245,
        "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
    }}
    usage = g._usage(payload)
    assert usage["input_tokens"] == 25
    assert usage["output_tokens"] == 1220          # thoughts are billed as output
    assert usage["output_tokens_details"]["image_tokens"] == 1120
    assert usage["output_tokens_details"]["text_tokens"] == 100


def test_usage_never_returns_null_when_the_vendor_omits_fields():
    usage = g._usage({})
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert isinstance(usage[key], int)
    assert usage["output_tokens_details"]["image_tokens"] == 0


def test_usage_without_modality_details_counts_all_output_as_image():
    """The chatfire gateway reports no candidatesTokensDetails."""
    usage = g._usage({"usageMetadata": {"promptTokenCount": 271,
                                       "candidatesTokenCount": 1485,
                                       "totalTokenCount": 1756}})
    assert usage["output_tokens"] == 1485
    assert usage["output_tokens_details"]["image_tokens"] == 1485
    assert usage["output_tokens_details"]["text_tokens"] == 0


# --- state table ------------------------------------------------------------


def test_remember_caps_the_state_table():
    for i in range(g._STATE_MAX + 100):
        g._remember(FakeCtx(), "b64_json")
        g._STATE[f"req-{i}"] = {"response_format": None}
    before = len(g._STATE)
    g._remember(FakeCtx(), "b64_json")
    assert len(g._STATE) <= before          # trimming keeps it from growing forever
    g._STATE.clear()
