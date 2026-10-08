"""fal/images@v1: the FLUX.3 direct-run script, specified.

The suite is organised by the decisions the script makes:

  * the endpoint: fal splits an app by operation, so a request carrying
    `image` goes to the `edit-image` twin of whatever path the channel
    carries (swap an existing suffix, append one to a bare app path), and
    a mapped model is a full fal endpoint id spliced onto the channel's
    own host;
  * the body: a whitelist rebuild -- prompt plus the documented optional
    knobs -- where `size` is *derived* into fal's aspect-ratio enum and
    resolution tier, junk sizes are dropped rather than refused, `n` is
    never forwarded, and `b64_json` forces `sync_mode: true` because the
    data URI is the only wire form carrying bytes;
  * the edit guards: a prompt is required, ten references are the cap, an
    explicitly empty list is an edit with its input forgotten, and both
    URL and data-URI references ride through verbatim;
  * the reply: `images` is rebuilt as `data` with `created` from the local
    clock and `seed` passed through, the url's own shape decides the
    conversion (data URI -> stored link or bare b64, http link -> verbatim,
    rehosted, or fetched-and-encoded), and a picture-less 200 fails
    loudly.

No live upstream is contacted anywhere in this file: every network-shaped
ctx method (object-storage upload, rehost, checked download) is a recorder,
and the fal JSON answers are fabrications. That proves the script speaks
the documented contract; the first live probe against real fal traffic is
still owed and is tracked in capabilities/fal.json's `unverified` note.
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "fal"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fal = _load("fal_images_v1", "images@v1.py")

T2I = "https://fal.run/blackforestlabs/flux-3/text-to-image"
EDIT = "https://fal.run/blackforestlabs/flux-3/edit-image"
BARE_APP = "https://fal.run/blackforestlabs/flux-3"
MAPPED_FULL = "https://fal.run/fal-ai/flux-pro/v1.1-ultra"
MAPPED_TWIN = "https://fal.run/fal-ai/flux-pro/edit-image"

REF = "https://cdn.example/cat.png"
REF2 = "https://cdn.example/dog.png"
FAL_LINK = "https://fal.media/files/abc/out.png"
OURS = "https://oss.example/temp/x.png"
RAW = b"\x89PNG-not-really-bytes"
BARE = base64.b64encode(RAW).decode()
DATA_URI = "data:image/png;base64," + BARE


class _ChannelStub:
    upstream_key = "keyid:keysecret"
    upstream_url = T2I
    stage_urls: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


class Recorder:
    """Stands in for object storage, the rehost and the checked download."""

    def __init__(self, stored: str = OURS) -> None:
        self.stored = stored
        self.uploads: list[bytes] = []
        self.rehosts: list[str] = []
        self.b64_fetches: list[str] = []

    async def upload(self, data: bytes, ext: str = "png") -> str:
        self.uploads.append(data)
        return self.stored

    async def rehost(self, url: str):
        self.rehosts.append(url)
        return None if self.stored is None else self.stored

    async def fetch_b64(self, ref: str) -> str:
        self.b64_fetches.append(ref)
        return BARE


def _ctx(monkeypatch, *, upstream_url: str = T2I, mapped_model: str | None = None,
         options: dict | None = None, stored: str = OURS) -> tuple[AdapterContext, Recorder]:
    channel = _ChannelStub(options)
    channel.upstream_url = upstream_url
    ctx = AdapterContext(
        request_id="req-fal-1",
        channel=channel,
        settings=Settings(minio_endpoint=""),
        mapped_model=mapped_model,
    )
    spy = Recorder(stored=stored)
    monkeypatch.setattr(ctx, "upload_temp_image", spy.upload)
    monkeypatch.setattr(ctx, "rehost_image", spy.rehost)
    monkeypatch.setattr(ctx, "image_b64", spy.fetch_b64)
    return ctx, spy


@pytest.fixture(autouse=True)
def _clean_state():
    fal._STATE.clear()
    yield
    fal._STATE.clear()


async def _request(ctx, payload: dict) -> dict:
    return await fal.transform(ctx, payload, "request")


async def _respond(ctx, payload: dict) -> dict:
    return await fal.transform(ctx, payload, "response")


# --------------------------------------------------------------------------
# The endpoint: one script, two fal apps
# --------------------------------------------------------------------------


async def test_t2i_keeps_channel_url(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat"})
    assert ctx.plan.url == T2I


async def test_image_selects_edit_twin(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "make it night", "image": REF})
    assert ctx.plan.url == EDIT


async def test_image_list_selects_edit_twin(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "combine", "image": [REF, REF2]})
    assert ctx.plan.url == EDIT


async def test_bare_app_path_gets_suffix_appended(monkeypatch):
    ctx, _ = _ctx(monkeypatch, upstream_url=BARE_APP)
    await _request(ctx, {"prompt": "a cat"})
    assert ctx.plan.url == BARE_APP + "/text-to-image"


async def test_edit_channel_base_swaps_back_for_t2i(monkeypatch):
    ctx, _ = _ctx(monkeypatch, upstream_url=EDIT)
    await _request(ctx, {"prompt": "a cat"})
    assert ctx.plan.url == T2I


async def test_mapped_model_used_verbatim_for_t2i(monkeypatch):
    ctx, _ = _ctx(monkeypatch, mapped_model="fal-ai/flux-pro/v1.1-ultra")
    await _request(ctx, {"prompt": "a cat"})
    assert ctx.plan.url == MAPPED_FULL


async def test_mapped_model_edit_twin_by_suffix(monkeypatch):
    ctx, _ = _ctx(monkeypatch, mapped_model="fal-ai/flux-pro/text-to-image")
    await _request(ctx, {"prompt": "edit", "image": REF})
    assert ctx.plan.url == MAPPED_TWIN


async def test_mapped_model_without_edit_twin_refuses_edit(monkeypatch):
    # A mapped id that names neither suffix has no twin this script can
    # name: the edit request fails rather than fabricating a path.
    ctx, _ = _ctx(monkeypatch, mapped_model="fal-ai/flux-pro/v1.1-ultra")
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"prompt": "edit", "image": REF})
    assert err.value.code == "channel_config_error"


# --------------------------------------------------------------------------
# The body: whitelist rebuild and the size derivation
# --------------------------------------------------------------------------


async def test_body_whitelist_drops_unknowns(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "n": 3, "quality": "hd",
                                "style": "vivid", "watermark": False})
    assert body == {"prompt": "a cat"}


async def test_size_derives_ratio_and_tier(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "size": "1024x1024"})
    assert body["aspect_ratio"] == "1:1"
    assert body["resolution"] == "1k"
    assert "size" not in body


@pytest.mark.parametrize(
    "size,ratio,tier",
    [
        # 512sq is not derivable: the live endpoint rejects it (2026-10-09),
        # so the derivation floor is 768sq.
        ("512x512", "1:1", "768sq"),
        ("768x768", "1:1", "768sq"),
        ("2048x2048", "1:1", "2k"),
        ("4096x4096", "1:1", "4k"),
        ("1920x1080", "16:9", "2k"),
        ("1080x1920", "9:16", "2k"),
        ("2048x1024", "2:1", "2k"),
        ("500x1000", "1:2", "1k"),
        ("2560x1080", "21:9", "2k"),
    ],
)
async def test_size_derivation_table(monkeypatch, size, ratio, tier):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "size": size})
    assert body["aspect_ratio"] == ratio
    assert body["resolution"] == tier


async def test_explicit_512sq_is_lifted_to_768sq(monkeypatch):
    # Documented upstream, rejected live: forwarded it would be a 422 the
    # caller cannot read (the engine carries no upstream body).
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "resolution": "512sq"})
    assert body["resolution"] == "768sq"


async def test_explicit_aspect_wins_but_tier_still_derived(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(
        ctx, {"prompt": "a cat", "size": "1024x1024", "aspect_ratio": "9:16"}
    )
    assert body["aspect_ratio"] == "9:16"
    assert body["resolution"] == "1k"


@pytest.mark.parametrize("size", ["abc", "1024", "-5x500", "0x512", "1024x", 1024, None])
async def test_unparseable_size_is_dropped_not_refused(monkeypatch, size):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "size": size})
    assert "aspect_ratio" not in body
    assert "resolution" not in body


async def test_non_string_enum_is_dropped(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "aspect_ratio": 7, "resolution": 2.5})
    assert "aspect_ratio" not in body
    assert "resolution" not in body


async def test_optional_knobs_only_when_present(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat"})
    for key in fal.PASSTHROUGH:
        assert key not in body
    body = await _request(
        ctx,
        {"prompt": "a cat", "safety_tolerance": 1, "enable_prompt_expansion": True,
         "output_format": "png", "version": "latest"},
    )
    assert body["safety_tolerance"] == 1
    assert body["enable_prompt_expansion"] is True
    assert body["output_format"] == "png"
    assert body["version"] == "latest"


# --------------------------------------------------------------------------
# sync_mode: how b64_json is honoured
# --------------------------------------------------------------------------


async def test_b64_request_forces_sync_mode(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "response_format": "b64_json"})
    assert body["sync_mode"] is True
    assert "response_format" not in body


async def test_b64_outranks_explicit_sync_false(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(
        ctx, {"prompt": "a cat", "response_format": "b64_json", "sync_mode": False}
    )
    assert body["sync_mode"] is True


async def test_explicit_sync_mode_honoured_without_b64(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "sync_mode": True})
    assert body["sync_mode"] is True


async def test_no_sync_mode_by_default(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat"})
    assert "sync_mode" not in body


# --------------------------------------------------------------------------
# The edit guards
# --------------------------------------------------------------------------


async def test_edit_without_prompt_is_refused(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"prompt": "", "image": REF})
    assert err.value.param == "prompt"


async def test_more_than_ten_references_refused(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    refs = [REF] * 11
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"prompt": "combine", "image": refs})
    assert err.value.param == "image"


async def test_ten_references_pass(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    refs = [REF] * 10
    body = await _request(ctx, {"prompt": "combine", "image": refs})
    assert body["image_urls"] == refs


async def test_empty_image_list_means_no_image(monkeypatch):
    # Canonical gate semantics: null / [] are dropped as "no image", so a
    # list that somehow reaches the script (a future gate change) reads as
    # text-to-image -- the script follows the payload's truth.
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "image": []})
    assert "image_urls" not in body
    assert ctx.plan.url == T2I


async def test_single_image_becomes_list(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "edit", "image": REF})
    assert body["image_urls"] == [REF]


async def test_data_uri_reference_rides_through(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "edit", "image": DATA_URI})
    assert body["image_urls"] == [DATA_URI]
    assert spy.uploads == []


# --------------------------------------------------------------------------
# The reply
# --------------------------------------------------------------------------


async def test_reply_maps_envelope(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat"})
    out = await _respond(
        ctx,
        {
            "images": [
                {
                    "url": FAL_LINK,
                    "width": 1024,
                    "height": 768,
                    "content_type": "image/png",
                    "file_name": "out.png",
                    "file_size": 12345,
                }
            ],
            "seed": 42,
        },
    )
    assert isinstance(out["created"], int)
    assert out["seed"] == 42
    assert out["data"] == [
        {"url": FAL_LINK, "width": 1024, "height": 768, "content_type": "image/png"}
    ]


async def test_pictureless_200_fails_loudly(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat"})
    with pytest.raises(AdapterError):
        await _respond(ctx, {"images": []})
    with pytest.raises(AdapterError):
        await _respond(ctx, {})


async def test_data_uri_becomes_stored_url_by_default(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat", "sync_mode": True})
    out = await _respond(ctx, {"images": [{"url": DATA_URI, "width": 64, "height": 64}]})
    assert out["data"][0]["url"] == OURS
    assert spy.uploads == [RAW]
    assert "b64_json" not in out["data"][0]


async def test_data_uri_becomes_bare_b64_when_asked(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat", "response_format": "b64_json"})
    out = await _respond(ctx, {"images": [{"url": DATA_URI}]})
    assert out["data"][0]["b64_json"] == BARE
    assert spy.uploads == []
    assert "url" not in out["data"][0]


async def test_data_uri_without_storage_degrades_to_b64(monkeypatch):
    ctx, spy = _ctx(monkeypatch, stored="not-a-url")
    await _request(ctx, {"prompt": "a cat", "sync_mode": True})
    out = await _respond(ctx, {"images": [{"url": DATA_URI}]})
    assert out["data"][0]["b64_json"] == BARE
    assert "url" not in out["data"][0]


async def test_http_link_encoded_when_b64_asked(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat", "response_format": "b64_json"})
    out = await _respond(ctx, {"images": [{"url": FAL_LINK}]})
    assert out["data"][0]["b64_json"] == BARE
    assert spy.b64_fetches == [FAL_LINK]


async def test_rehost_swaps_link(monkeypatch):
    ctx, spy = _ctx(monkeypatch, options={"rehost_url": True})
    await _request(ctx, {"prompt": "a cat"})
    out = await _respond(ctx, {"images": [{"url": FAL_LINK}]})
    assert out["data"][0]["url"] == OURS
    assert spy.rehosts == [FAL_LINK]


async def test_rehost_without_storage_keeps_vendor_link(monkeypatch):
    ctx, spy = _ctx(monkeypatch, options={"rehost_url": True}, stored=None)
    await _request(ctx, {"prompt": "a cat"})
    out = await _respond(ctx, {"images": [{"url": FAL_LINK}]})
    assert out["data"][0]["url"] == FAL_LINK
    assert spy.rehosts == [FAL_LINK]


async def test_no_rehost_by_default(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat"})
    out = await _respond(ctx, {"images": [{"url": FAL_LINK}]})
    assert out["data"][0]["url"] == FAL_LINK
    assert spy.rehosts == []


async def test_parked_format_consumed_once(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "a cat", "response_format": "b64_json"})
    out = await _respond(ctx, {"images": [{"url": DATA_URI}]})
    assert out["data"][0]["b64_json"] == BARE
    # A second reply on the same ctx finds no parked entry: the format was
    # consumed by the first, so a plain link now rides through untouched.
    out = await _respond(ctx, {"images": [{"url": FAL_LINK}]})
    assert out["data"][0]["url"] == FAL_LINK
    assert fal._STATE.get(ctx.request_id) is None
