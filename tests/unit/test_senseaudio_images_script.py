"""senseaudio/images@v1: the SenseAudio sync script, specified.

The suite is organised by the decisions the script makes:

  * the model: `doubao-seedream-*` folds to the platform's only seedream,
    an X-Model-Map match outranks the fold, and every other name -- unknown
    or absent -- falls back to the seedream too: an unknown name spent on
    the upstream buys a 502 whose body the engine does not carry, spent on
    the seedream it buys a picture;
  * the size: exact table hits ride through, off-table sizes land on the
    intent-preserving neighbour (same aspect ratio by nearest area, else
    log distance within the caller's orientation -- a portrait request
    never comes back landscape), and missing/junk sizes fall back to the
    table's cheapest entry -- the live upstream wants a size even with a
    reference, and the folding doors carry none, so a fallback is what
    keeps three of the four front doors alive;
  * the body: a whitelist rebuild -- prompt (always required upstream),
    a single reference (the wire field is one string; a list is refused
    with the count, never truncated), and a real-int seed (booleans are
    Python ints and read as a typo);
  * the reply: the bare `{"url": ...}` becomes the canonical envelope --
    http links ride through (verbatim, rehosted, or fetched-and-encoded),
    a data URI in the url slot is stored or degraded to b64_json, a
    picture-less 200 fails loudly, and the parked response_format is
    consumed exactly once.

No live upstream is contacted anywhere in this file: every network-shaped
ctx method is a recorder, and the upstream JSON answers are fabrications.
That proves the script speaks the documented contract; the live probes of
2026-10-10 (seven + thirty-one billable generations, byte-checked) live in
capabilities/senseaudio.json.
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "senseaudio"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sa = _load("senseaudio_images_v1", "images@v1.py")

UPSTREAM = "https://api.senseaudio.cn/v1/image/sync"
IMAGE2 = "senseaudio-image-2.0-260319"
SEEDREAM = sa.SEEDREAM_CANONICAL
U1 = "sensenova-u1-fast"

REF = "https://cdn.example/cat.png"
REF2 = "https://cdn.example/dog.png"
SA_LINK = "https://dynamic.senseaudio.cn/image/abc-123"
OURS = "https://oss.example/temp/x.png"
RAW = b"\xff\xd8-not-really-jpeg"
BARE = base64.b64encode(RAW).decode()
DATA_URI = "data:image/jpeg;base64," + BARE


class _ChannelStub:
    upstream_key = "sk-senseaudio-test"
    upstream_url = UPSTREAM
    stage_urls: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


class Recorder:
    """Stands in for object storage, the rehost and the checked download."""

    def __init__(self, stored: str | None = OURS) -> None:
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

    def decode(self, uri: str) -> bytes:
        return RAW

    def encode(self, raw: bytes) -> str:
        return BARE


def _ctx(monkeypatch, *, mapped_model: str | None = None,
         options: dict | None = None, stored: str | None = OURS) -> tuple[AdapterContext, Recorder]:
    ctx = AdapterContext(
        request_id="req-sa-1",
        channel=_ChannelStub(options),
        settings=Settings(minio_endpoint=""),
        mapped_model=mapped_model,
    )
    spy = Recorder(stored=stored)
    monkeypatch.setattr(ctx, "upload_temp_image", spy.upload)
    monkeypatch.setattr(ctx, "rehost_image", spy.rehost)
    monkeypatch.setattr(ctx, "image_b64", spy.fetch_b64)
    monkeypatch.setattr(ctx, "decode_b64", spy.decode)
    monkeypatch.setattr(ctx, "encode_b64", spy.encode)
    return ctx, spy


@pytest.fixture(autouse=True)
def _clean_state():
    sa._STATE.clear()
    yield
    sa._STATE.clear()


async def _request(ctx, payload: dict) -> dict:
    return await sa.transform(ctx, payload, "request")


async def _respond(ctx, payload: dict) -> dict:
    return await sa.transform(ctx, payload, "response")


# --------------------------------------------------------------------------
# The model: fold, map, verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "caller",
    ["doubao-seedream-5-0-260128", "doubao-seedream-4-0-250828",
     "doubao-seedream-3-0-t2i-250415", "doubao-seedream"],
)
async def test_seedream_prefix_folds(monkeypatch, caller):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": caller, "prompt": "a cat",
                                "size": "2304x1728"})
    assert body["model"] == SEEDREAM


async def test_image2_and_u1_ride_verbatim(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1024x1024"})
    assert body["model"] == IMAGE2
    body = await _request(ctx, {"model": U1, "prompt": "a cat",
                                "size": "2048x2048"})
    assert body["model"] == U1


async def test_unknown_model_falls_back_to_seedream(monkeypatch):
    # An unknown name spent on the upstream buys a 502 whose body the engine
    # does not carry; spent on the seedream it buys a picture. The fallback
    # sizes against the seedream table like any other seedream request.
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": "senseaudio-image-3-0-future",
                                "prompt": "a cat", "size": "1024x1024"})
    assert body["model"] == SEEDREAM
    assert body["size"] == "2048x2048"


async def test_mapped_model_outranks_fold(monkeypatch):
    ctx, _ = _ctx(monkeypatch, mapped_model="senseaudio-image-2.0-260319")
    body = await _request(ctx, {"model": "doubao-seedream-4-0", "prompt": "a cat",
                                "size": "1024x1024"})
    assert body["model"] == IMAGE2


async def test_missing_model_falls_back_to_seedream(monkeypatch):
    # Even an absent name falls back rather than refusing: this channel's
    # promise is a picture, and an absent name is a routing question.
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"prompt": "a cat", "size": "1024x1024"})
    assert body["model"] == SEEDREAM
    assert body["size"] == "2048x2048"


# --------------------------------------------------------------------------
# The size: exact, same-shape, oriented log-neighbour, junk
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model,size",
    [(IMAGE2, "1024x1024"), (IMAGE2, "3136x1344"), (SEEDREAM, "2304x1728"),
     (SEEDREAM, "4704x2016"), (U1, "1664x2496"), (U1, "3072x1376")],
)
async def test_table_hits_ride_through(monkeypatch, model, size):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": model, "prompt": "a cat", "size": size})
    assert body["size"] == size


@pytest.mark.parametrize(
    "model,size,expected",
    [
        # Same aspect ratio wins by nearest pixel area: the square is the
        # point of 1024x1024, so seedream's only square is the neighbour --
        # not the closer-in-area 2304x1728.
        (SEEDREAM, "1024x1024", "2048x2048"),
        (SEEDREAM, "1920x1080", "4096x2304"),
        (SEEDREAM, "400x300", "2304x1728"),
        (IMAGE2, "1920x1080", "2048x1152"),
        (IMAGE2, "2560x1440", "2048x1152"),
        (U1, "1024x1024", "2048x2048"),
        # Log distance, within the caller's orientation: a portrait request
        # never lands on a landscape neighbour (300x1700 must not come back
        # as 2304x1728 -- log would let a small height gap outvote a 6x
        # width gap).
        (SEEDREAM, "500x300", "2304x1728"),
        (SEEDREAM, "300x1700", "1728x2304"),
        (U1, "1080x1920", "1824x2272"),
        # The square has exactly one same-shape candidate on image-2.0, and
        # fidelity to the shape outranks fidelity to the pixel count.
        (IMAGE2, "3000x3000", "1024x1024"),
    ],
)
async def test_off_table_sizes_lands_on_neighbour(monkeypatch, model, size, expected):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": model, "prompt": "a cat", "size": size})
    assert body["size"] == expected


async def test_mapped_unknown_model_keeps_verbatim_size(monkeypatch):
    # A mapped value is an explicit operator decision: used verbatim, never
    # second-guessed -- and with no size table behind it, its size rides
    # through for the upstream to judge.
    ctx, _ = _ctx(monkeypatch, mapped_model="future-model-x")
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1234x567"})
    assert body["model"] == "future-model-x"
    assert body["size"] == "1234x567"


@pytest.mark.parametrize("size", ["abc", "1024", "-5x500", "0x512", "1024x", 1024, None])
async def test_missing_or_junk_size_falls_back_to_smallest(monkeypatch, size):
    # The live upstream wants a size even with a reference (400
    # "参数错误：size", 2026-10-10), and the chat/responses doors carry
    # none: the cheapest table entry is what stands in for "auto".
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": size, "image": REF})
    assert body["size"] == "1024x1024"
    assert body["reference"] == REF


async def test_size_fallback_is_per_model(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": SEEDREAM, "prompt": "a cat"})
    assert body["size"] == "2304x1728"
    body = await _request(ctx, {"model": U1, "prompt": "a cat"})
    # 1824x2272 (4.144M px) edges out 1664x2496 (4.153M) for the cheapest
    # u1 entry -- picked by area, not by document order.
    assert body["size"] == "1824x2272"


@pytest.mark.parametrize(
    "model,tier,expected",
    [
        # Long edge nearest K*1024, shape-blind: "4K" lands on the 4096-long
        # entry (4096x2304 wins the tie with 2304x4096 by table order),
        # never on the same-shape 2048x2048 the aspect rules would pick.
        (SEEDREAM, "4K", "4096x2304"),
        (SEEDREAM, "4k", "4096x2304"),
        (SEEDREAM, "2k", "2048x2048"),
        (SEEDREAM, "1k", "2048x2048"),  # smallest long edge the table has
        (IMAGE2, "1K", "1024x1024"),
        (IMAGE2, "4k", "3136x1344"),  # table tops out at 3136
        (U1, "2k", "2048x2048"),
    ],
)
async def test_tier_words(monkeypatch, model, tier, expected):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": model, "prompt": "a cat", "size": tier})
    assert body["size"] == expected


async def test_tier_word_with_whitespace_is_honoured(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": SEEDREAM, "prompt": "a cat", "size": " 4K "})
    assert body["size"] == "4096x2304"


async def test_mapped_model_without_size_is_refused(monkeypatch):
    # A mapped model with no table has no fallback to offer: nothing to
    # repair with, and the upstream's verdict is the only one left.
    ctx, _ = _ctx(monkeypatch, mapped_model="future-model-x")
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"model": IMAGE2, "prompt": "a cat"})
    assert err.value.param == "size"


# --------------------------------------------------------------------------
# The body: prompt, reference, seed, whitelist
# --------------------------------------------------------------------------


async def test_empty_prompt_is_refused_even_with_reference(monkeypatch):
    # prompt is required upstream in both modes, and senseaudio's 400s
    # carry no field detail: here is the only place it gets named.
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"model": IMAGE2, "prompt": "", "image": REF})
    assert err.value.param == "prompt"


async def test_reference_rides_through_with_explicit_size(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": IMAGE2, "prompt": "make it night",
                                "size": "1024x1024", "image": DATA_URI})
    assert body["reference"] == DATA_URI
    assert body["size"] == "1024x1024"


async def test_two_references_are_refused_not_truncated(monkeypatch):
    # The wire field is a single string; dropping the second would pass off
    # a different picture as the answer to the two the caller wrote.
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as err:
        await _request(ctx, {"model": IMAGE2, "prompt": "combine",
                             "size": "1024x1024", "image": [REF, REF2]})
    assert err.value.param == "image"
    assert "2" in err.value.message


async def test_seed_int_rides_bool_and_str_dropped(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1024x1024", "seed": 42})
    assert body["seed"] == 42
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1024x1024", "seed": True})
    assert "seed" not in body
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1024x1024", "seed": "42"})
    assert "seed" not in body


async def test_body_whitelist_drops_unknowns(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    body = await _request(ctx, {"model": IMAGE2, "prompt": "a cat",
                                "size": "1024x1024", "n": 3, "quality": "hd",
                                "style": "vivid", "watermark": False,
                                "response_format": "url"})
    assert body == {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"}


# --------------------------------------------------------------------------
# The reply: the bare url becomes the canonical envelope
# --------------------------------------------------------------------------


async def test_reply_maps_envelope(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert isinstance(out["created"], int)
    assert out["data"] == [{"url": SA_LINK}]


async def test_pictureless_200_fails_loudly(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    with pytest.raises(AdapterError):
        await _respond(ctx, {})
    with pytest.raises(AdapterError):
        await _respond(ctx, {"url": ""})


async def test_b64_request_fetches_and_encodes(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024",
                         "response_format": "b64_json"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"b64_json": BARE}]
    assert spy.b64_fetches == [SA_LINK]


async def test_rehost_swaps_link(monkeypatch):
    ctx, spy = _ctx(monkeypatch, options={"rehost_url": True})
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"url": OURS}]
    assert spy.rehosts == [SA_LINK]


async def test_rehost_without_storage_keeps_vendor_link(monkeypatch):
    ctx, spy = _ctx(monkeypatch, options={"rehost_url": True}, stored=None)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"url": SA_LINK}]
    assert spy.rehosts == [SA_LINK]


async def test_no_rehost_by_default(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"url": SA_LINK}]
    assert spy.rehosts == []


async def test_data_uri_in_url_slot_is_stored(monkeypatch):
    # Never observed on this upstream, defended anyway: a data URI is never
    # parked in `url`, which is not a URL.
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": DATA_URI})
    assert out["data"] == [{"url": OURS}]
    assert spy.uploads == [RAW]


async def test_data_uri_without_storage_degrades_to_b64(monkeypatch):
    ctx, spy = _ctx(monkeypatch, stored="not-a-url")
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024"})
    out = await _respond(ctx, {"url": DATA_URI})
    assert out["data"] == [{"b64_json": BARE}]
    assert "url" not in out["data"][0]


async def test_parked_format_consumed_once(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"model": IMAGE2, "prompt": "a cat", "size": "1024x1024",
                         "response_format": "b64_json"})
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"b64_json": BARE}]
    # A second reply on the same ctx finds no parked entry: the format was
    # consumed by the first, so a plain link now rides through untouched.
    out = await _respond(ctx, {"url": SA_LINK})
    assert out["data"] == [{"url": SA_LINK}]
    assert sa._STATE.get(ctx.request_id) is None
