"""volcengine_ark/images@v1: one script, and every behaviour it has.

This is the channel's only version. The version-split practice was dropped, so
nothing below is a comparison between two files -- each class states what the
shipped script does, and the assertions are the specification a change has to
keep passing.

The shape of a request is decided by three things and the suite is organised by
them:

  * `image_ref_mode` picks the wire form for every reference (including URLs,
    which get no privileged exemption -- that exemption is what used to let a
    slow source time out inside ARK);
  * the `ref_*` group optionally re-encodes a reference whose bytes pass
    through us, and its three failure rules each have a test here because each
    of them is a way an optimisation turns a working request into a 400;
  * ARK's own fetch cap is answered by an override on the *retry* attempt, and
    that override must win over whatever mode was configured.

Two assertions are pinned rather than blessed: an unrecognised `image_ref_mode`
falls through to the URL path, and a `null` watermark is forwarded as `null`.
Both are recorded so a change to them is deliberate.
"""

from __future__ import annotations

import base64
import importlib.util
import io
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "volcengine_ark"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ark = _load("ark_images_v1_main", "images@v1.py")

#: Only the magic bytes matter for anything that is not decoded. The `ref_*`
#: tests need a payload Pillow can open, which is what REAL_PNG is for -- and the
#: fact that this stub survives them proves the fallback path: a reference the
#: pipeline cannot read is sent on rather than refused.
PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"
BARE = base64.b64encode(PNG).decode()
DATA_URI = f"data:image/png;base64,{BARE}"


def _real_png(width: int = 400, height: int = 300) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


REAL_PNG = _real_png()
REAL_BARE = base64.b64encode(REAL_PNG).decode()


def _size_of(payload: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(payload)) as img:
        return img.size


def _b64_size(value: str) -> tuple[int, int]:
    return _size_of(base64.b64decode(value.split(";base64,", 1)[-1]))


#: A stand-in for the case that motivated the wire-form option: a source ARK
#: cannot pull inside its own 5 s budget.
URL = "https://overseas.example/holiday.png"

OTHER_URL = "https://overseas.example/second.png"

REHOSTED = "https://our-storage.example/temp.png"


class _ChannelStub:
    """Only the attributes ContextCore copies. No channel semantics involved."""

    upstream_key = "ark-key"
    upstream_url = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
    stage_urls: ClassVar[dict] = {}

    def __init__(self, options: dict) -> None:
        self.options = options


class Spy:
    """Records the two things a request can do to the client's image.

    "Did we fetch it" and "did we upload it" are the whole observable
    difference between the modes, so they are recorded rather than inferred
    from the body: a body that happens to look right must not pass while the
    script quietly made the wrong call. The uploaded *bytes* and their
    extension are recorded for the same reason -- a compressed reference is a
    different picture, and the extension is what the object store is told it
    is.
    """

    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.uploads: list[bytes] = []
        self.exts: list[str] = []


def _ctx(monkeypatch, payload: bytes = PNG, **options) -> tuple[AdapterContext, Spy]:
    ctx = AdapterContext(
        request_id="req-1",
        channel=_ChannelStub(options),
        settings=Settings(minio_endpoint=""),
    )
    spy = Spy()

    async def fake_download(url: str) -> bytes:
        spy.downloads.append(url)
        return payload

    async def fake_upload(data: bytes, ext: str = "png") -> str:
        spy.uploads.append(data)
        spy.exts.append(ext)
        return REHOSTED

    monkeypatch.setattr(ctx, "download_image", fake_download)
    monkeypatch.setattr(ctx, "upload_temp_image", fake_upload)
    return ctx, spy


async def _image_field(ctx, image, module=ark):
    """The `image` field after the request phase."""
    body = await module.transform(
        ctx, {"prompt": "replace the circle", "image": image}, "request"
    )
    return body["image"]


async def _body(ctx, payload: dict, module=ark):
    return await module.transform(ctx, payload, "request")


class TestDefaultMode:
    """`url` mode: the cheap path, and the one thing it must not touch."""

    @pytest.mark.asyncio
    async def test_a_url_is_forwarded_verbatim_and_costs_us_nothing(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch)
        assert await _image_field(ctx, URL) == URL
        assert spy.downloads == []
        assert spy.uploads == []

    @pytest.mark.asyncio
    async def test_an_inline_reference_is_rehosted_because_ark_wants_a_url(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch)
        assert await _image_field(ctx, BARE) == REHOSTED
        assert spy.uploads == [PNG]
        assert spy.downloads == []


class TestDataUriMode:
    """The mode that stops ARK from fetching anything at all."""

    @pytest.mark.asyncio
    async def test_a_url_is_fetched_by_us_and_inlined(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        assert await _image_field(ctx, URL) == DATA_URI
        assert spy.downloads == [URL]

    @pytest.mark.asyncio
    async def test_the_url_is_never_what_ark_receives(self, monkeypatch):
        """The regression, stated as the thing that must not happen.

        Under the old ordering this assertion is the one that fails: the raw
        URL was returned before the mode was read, so ARK was handed the very
        source it then timed out on.
        """
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        image = await _image_field(ctx, URL)
        assert URL not in str(image)
        assert image.startswith("data:image/png;base64,")
        assert spy.downloads == [URL]

    @pytest.mark.asyncio
    async def test_an_inline_reference_stays_inline_and_touches_no_network(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        assert await _image_field(ctx, DATA_URI) == DATA_URI
        assert spy.downloads == []
        assert spy.uploads == []

    @pytest.mark.asyncio
    async def test_every_reference_in_an_array_is_inlined(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        assert await _image_field(ctx, [URL, BARE]) == [DATA_URI, DATA_URI]
        assert spy.downloads == [URL]

    @pytest.mark.asyncio
    async def test_inline_is_accepted_as_the_google_scripts_name_for_it(
        self, monkeypatch
    ):
        """An operator who learned the value on a google channel gets it here.

        Falling through to "url" instead would be a silent no-op, which is the
        exact defect this option exists to fix.
        """
        ctx, spy = _ctx(monkeypatch, image_ref_mode="inline")
        assert await _image_field(ctx, URL) == DATA_URI
        assert spy.downloads == [URL]


class TestBase64Mode:
    @pytest.mark.asyncio
    async def test_a_url_is_fetched_then_sent_without_a_prefix(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="base64")
        assert await _image_field(ctx, URL) == BARE
        assert spy.downloads == [URL]


class TestAnUnknownModeFallsThroughToUrl:
    """Pinned deliberately: this is the old behaviour, not a new failure mode.

    A channel carrying a value this script does not know must keep working
    rather than start refusing requests -- but note what "keep working" means
    for a URL, which is a request that hands ARK something to fetch and
    therefore gambles on its 5 s cap. That is why the fallthrough is stated
    here rather than left implicit.
    """

    @pytest.mark.asyncio
    async def test_a_url_is_still_passed_through(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data-url")
        assert await _image_field(ctx, URL) == URL
        assert spy.downloads == []

    @pytest.mark.asyncio
    async def test_an_inline_reference_is_rehosted(self, monkeypatch):
        """The fallthrough is not a no-op: it is `url` mode by another route."""
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data-url")
        assert await _image_field(ctx, BARE) == REHOSTED
        assert spy.uploads == [PNG]


class TestRefPolicy:
    """The `ref_*` options: what they shrink, and what they refuse to touch.

    Every case here is about a decision that could go quietly wrong. A policy
    that does not apply to one input shape, a policy that turns a request into
    a 400, a policy that reports the operator's mistake against the client's
    file -- all three are worse than no policy at all, and all three have a
    test below.
    """

    @pytest.mark.asyncio
    async def test_a_ceiling_shrinks_the_reference_before_it_is_uploaded(
        self, monkeypatch
    ):
        ctx, spy = _ctx(monkeypatch, payload=REAL_PNG, ref_max_edge=64)
        assert await _image_field(ctx, REAL_BARE) == REHOSTED
        assert _size_of(spy.uploads[0]) == (64, 48)
        assert spy.uploads[0] != REAL_PNG

    @pytest.mark.asyncio
    async def test_the_uploaded_extension_follows_the_format_we_chose(
        self, monkeypatch
    ):
        """The extension is the object store's contract, and the evidence.

        It is also how an operator can tell from the `storage_put` span that the
        policy ran at all, so it is not decoration.
        """
        ctx, spy = _ctx(monkeypatch, payload=REAL_PNG, ref_fmt="jpeg")
        await _image_field(ctx, REAL_BARE)
        assert spy.exts == ["jpeg"]

    @pytest.mark.asyncio
    async def test_a_url_in_url_mode_is_never_fetched_in_order_to_be_shrunk(
        self, monkeypatch
    ):
        """The one exemption, stated rather than discovered.

        Fetching a reference to compress it would spend exactly the 5 s gamble
        that `image_ref_mode: "url"` exists to avoid, so the policy cannot apply
        here -- and the operator is told so in the module docstring instead of
        finding out from a timeout.
        """
        ctx, spy = _ctx(monkeypatch, ref_max_edge=64)
        assert await _image_field(ctx, URL) == URL
        assert spy.downloads == []
        assert spy.uploads == []

    @pytest.mark.asyncio
    async def test_the_policy_applies_in_data_uri_mode(self, monkeypatch):
        """No input shape is exempt, which is the invariant this group keeps."""
        ctx, spy = _ctx(
            monkeypatch, payload=REAL_PNG, image_ref_mode="data_uri", ref_max_edge=64
        )
        image = await _image_field(ctx, URL)
        assert spy.downloads == [URL]
        assert image.startswith("data:image/png;base64,")
        assert _b64_size(image) == (64, 48)

    @pytest.mark.asyncio
    async def test_the_policy_applies_in_base64_mode(self, monkeypatch):
        ctx, spy = _ctx(
            monkeypatch, payload=REAL_PNG, image_ref_mode="base64", ref_max_edge=64
        )
        image = await _image_field(ctx, URL)
        assert not image.startswith("data:")
        assert _b64_size(image) == (64, 48)

    @pytest.mark.asyncio
    async def test_every_reference_in_an_array_is_shrunk(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, payload=REAL_PNG, ref_max_edge=64)
        image = await _image_field(ctx, [REAL_BARE, REAL_BARE, REAL_BARE])
        assert image == [REHOSTED, REHOSTED, REHOSTED]
        assert [_size_of(data) for data in spy.uploads] == [(64, 48)] * 3

    @pytest.mark.asyncio
    async def test_a_reference_we_cannot_decode_is_uploaded_as_it_arrived(
        self, monkeypatch
    ):
        """The fallback that stops an optimisation becoming a 400.

        The stub PNG is accepted by the front door and cannot be opened by
        Pillow, which is the shape a BMP or AVIF reference has from in here. It
        must reach ARK exactly as the client sent it.
        """
        ctx, spy = _ctx(monkeypatch, ref_max_edge=64)
        assert await _image_field(ctx, BARE) == REHOSTED
        assert spy.uploads == [PNG]
        assert spy.exts == ["png"]

    @pytest.mark.asyncio
    async def test_a_numeric_option_may_be_written_as_a_digit_string(
        self, monkeypatch
    ):
        ctx, spy = _ctx(
            monkeypatch,
            payload=REAL_PNG,
            ref_max_edge="64",
            ref_fmt="webp",
            ref_quality="85",
        )
        await _image_field(ctx, REAL_BARE)
        assert spy.exts == ["webp"]
        assert _size_of(spy.uploads[0]) == (64, 48)

    @pytest.mark.asyncio
    async def test_nothing_is_spent_when_no_policy_is_set(self, monkeypatch):
        ctx, spy = _ctx(monkeypatch, payload=REAL_PNG)
        assert await _image_field(ctx, REAL_BARE) == REHOSTED
        assert spy.uploads == [REAL_PNG]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "options",
        [
            {"ref_fmt": "tiff"},
            {"ref_max_edge": 0},
            {"ref_max_edge": -1},
            {"ref_max_edge": "abc"},
            {"ref_max_edge": True},
            {"ref_max_edge": 1.5},
            {"ref_max_bytes": 0},
            {"ref_quality": 85},  # cannot apply: no format named
            {"ref_quality": 85, "ref_fmt": "png"},  # named, and has no knob
            {"ref_quality": 500, "ref_fmt": "webp"},
            {"ref_quality": 1.5, "ref_fmt": "webp"},
        ],
    )
    async def test_a_malformed_option_is_a_channel_error_not_a_client_error(
        self, monkeypatch, options
    ):
        """`channel_config_error` says "the headers are wrong", which they are.

        A 400 carrying `image_invalid` would send whoever reads it to the
        client's file, and the client cannot fix a typo in `X-Channel-Options`.
        The refusal also happens before any upstream call, so it costs nothing
        but the operator's attention.
        """
        ctx, _ = _ctx(monkeypatch, **options)
        with pytest.raises(AdapterError) as excinfo:
            await _image_field(ctx, REAL_BARE)
        assert excinfo.value.code == "channel_config_error"

    @pytest.mark.asyncio
    async def test_a_malformed_option_does_not_break_text_to_image(
        self, monkeypatch
    ):
        """A reference policy is not consulted when there is no reference.

        Failing a text-to-image request over an option that cannot apply to it
        would make the operator's typo into the client's outage.
        """
        ctx, spy = _ctx(monkeypatch, ref_fmt="tiff")
        body = await _body(ctx, {"prompt": "a red circle"})
        assert "image" not in body


class TestRetry:
    """The answer to ARK's own fetch timeout, and its judgement.

    Two things have to hold for the retry to be worth having, and both are
    negative: the first attempt must still take the cheap path, and the second
    must actually take the different one. The judgement about *which* failure is
    worth answering lives here too -- the engine offers the extra request phase
    unconditionally and the script reads the message.
    """

    def test_both_wordings_are_named(self):
        assert ark.REFERENCE_FAILURES == (
            "Timeout while downloading url=",
            "invalid url specified",
        )

    @pytest.mark.asyncio
    async def test_the_first_attempt_still_takes_the_cheap_path(self, monkeypatch):
        """No `ctx.upstream_error` means no retry is in progress, so the URL is
        forwarded and we download nothing."""
        ctx, spy = _ctx(monkeypatch)
        assert await _image_field(ctx, URL) == URL
        assert spy.downloads == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        ["Timeout while downloading url=https://x/y", "invalid url specified."],
        ids=["could-not-fetch", "would-not-parse"],
    )
    async def test_a_retry_attempt_inlines_the_reference(self, monkeypatch, reason):
        """The answer to the failure: ARK gets no URL to fetch this time.

        Both wordings are answered, because they are the same refusal reached
        two ways: ARK gave up on the download, or would not accept the value as
        a URL at all.
        """
        ctx, spy = _ctx(monkeypatch)
        ctx.upstream_error = {
            "message": f"Upstream returned 400: The parameter `image` ... {reason}",
            "upstream_status": 400,
        }
        assert await _image_field(ctx, URL) == DATA_URI
        assert spy.downloads == [URL]

    @pytest.mark.asyncio
    async def test_a_failure_it_does_not_recognise_is_left_alone(self, monkeypatch):
        """The judgement, at its most important point.

        A content refusal arrives as the same 400 with different wording. On
        something this script cannot fix, inlining would change the request for
        no reason -- and the changed body would buy a second generation to
        collect the same refusal. Declining keeps the request identical, which is
        what lets the engine withhold the second call.
        """
        ctx, spy = _ctx(monkeypatch)
        ctx.upstream_error = {
            "message": "Upstream returned 400: The parameter `prompt` is not valid",
            "upstream_status": 400,
        }
        assert await _image_field(ctx, URL) == URL
        assert spy.downloads == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["base64", "url", "data-url"])
    async def test_a_retry_attempt_overrides_whatever_mode_was_configured(
        self, monkeypatch, mode
    ):
        """The override is read before the `url` fast path, and it wins.

        `base64` is the form measured to be rejected, so a retry must not spend
        itself on it; a URL must not be handed over again for the same reason.
        Both are the same rule: on a retry, the wire form is not the caller's to
        choose.
        """
        ctx, spy = _ctx(monkeypatch, image_ref_mode=mode)
        ctx.upstream_error = {
            "message": "Upstream returned 400: Timeout while downloading url=https://x",
            "upstream_status": 400,
        }
        assert await _image_field(ctx, URL) == DATA_URI
        assert spy.downloads == [URL]

    @pytest.mark.asyncio
    async def test_the_retry_still_shrinks_when_a_policy_is_set(self, monkeypatch):
        """A retry inlines, and an inlined reference is one whose bytes we hold
        -- so the compression policy applies to it like any other."""
        ctx, spy = _ctx(monkeypatch, payload=REAL_PNG, ref_max_edge=64)
        ctx.upstream_error = {
            "message": "Upstream returned 400: Timeout while downloading url=https://x",
            "upstream_status": 400,
        }
        image = await _image_field(ctx, URL)
        assert image.startswith("data:image/png;base64,")
        assert _b64_size(image) == (64, 48)


class TestWatermarkIsOffByDefault:
    """What leaves the adapter when nobody mentions watermark."""

    @pytest.mark.asyncio
    async def test_a_bare_request_asks_for_no_watermark(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle"})
        assert body["watermark"] is False

    @pytest.mark.asyncio
    async def test_the_field_is_sent_rather_than_left_out(self, monkeypatch):
        """Omitting it would mean "on", which is the bug, not the fix.

        ARK defaults this parameter to `true`, so the only way to turn it off is
        to say so. This assertion is what stops a later "tidy-up" that drops a
        field it reads as redundant from silently restoring the mark.
        """
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle"})
        assert "watermark" in body
        assert body["watermark"] is False

    @pytest.mark.asyncio
    async def test_a_channel_option_of_false_is_the_same_as_unset(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch, watermark=False)
        body = await _body(ctx, {"prompt": "a red circle"})
        assert body["watermark"] is False

    @pytest.mark.asyncio
    async def test_the_request_body_can_ask_for_it(self, monkeypatch):
        """The default is reversed; the capability is not removed."""
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle", "watermark": True})
        assert body["watermark"] is True

    @pytest.mark.asyncio
    async def test_the_channel_option_can_ask_for_it(self, monkeypatch):
        """A channel that needs the mark sets it once, not per request."""
        ctx, _ = _ctx(monkeypatch, watermark=True)
        body = await _body(ctx, {"prompt": "a red circle"})
        assert body["watermark"] is True

    @pytest.mark.asyncio
    async def test_the_body_still_wins_over_the_channel_option(self, monkeypatch):
        """The precedence chain runs payload > channel option > default.

        A channel that turned the mark on for its own reasons must not be able
        to override a caller who asked for a clean image -- otherwise the
        default would move the decision away from the caller rather than away
        from the vendor's default.
        """
        ctx, _ = _ctx(monkeypatch, watermark=True)
        body = await _body(ctx, {"prompt": "a red circle", "watermark": False})
        assert body["watermark"] is False


class TestANullIsNotAWayToSayUnset:
    """Pinned, not blessed: an explicit `null` is forwarded as `null`.

    The field is taken from the body verbatim when the key is present, and
    `dict.get` only falls back to the default when the key is *missing* -- so
    `{"watermark": null}`, which several SDKs emit for an unset boolean, reaches
    the vendor as `null` rather than as the default. Whether the front door
    should normalise that, the way it already does for `image` and
    `response_format`, is an open decision; this test records today's behaviour
    so a change to it is deliberate.
    """

    @pytest.mark.asyncio
    async def test_a_null_reaches_the_upstream_as_a_null(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle", "watermark": None})
        assert "watermark" in body
        assert body["watermark"] is None


class TestTheWireShape:
    """How the `image` field is shaped on its way out."""

    @pytest.mark.asyncio
    async def test_an_empty_image_key_is_treated_as_absent(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle", "image": []})
        assert "image" not in body

    @pytest.mark.asyncio
    async def test_a_single_element_array_collapses_to_a_scalar(self, monkeypatch):
        """ARK takes one reference as a scalar; the array is the caller's shape,
        not the wire form."""
        ctx, _ = _ctx(monkeypatch)
        assert await _image_field(ctx, [URL]) == URL

    @pytest.mark.asyncio
    async def test_two_url_references_stay_an_array(self, monkeypatch):
        ctx, _ = _ctx(monkeypatch)
        assert await _image_field(ctx, [URL, OTHER_URL]) == [URL, OTHER_URL]

    @pytest.mark.asyncio
    async def test_the_body_carries_the_vendor_defaults(self, monkeypatch):
        """The fields this script always sends, and the two it never does.

        `stream` is always off (the adapter is synchronous by contract) and
        `response_format` defaults to a link, since ARK honours both values and
        the caller asking for nothing should not be routed through storage.
        """
        ctx, _ = _ctx(monkeypatch)
        body = await _body(ctx, {"prompt": "a red circle"})
        assert body["model"] == "doubao-seedream-5-0-260128"
        assert body["stream"] is False
        assert body["response_format"] == "url"
        assert "sequential_image_generation" not in body

    @pytest.mark.asyncio
    async def test_a_passthrough_field_only_travels_when_it_is_supplied(
        self, monkeypatch
    ):
        """`sequential_image_generation` is model-gated: the 5-0-pro variants
        reject it with a 400, so it is forwarded on request and never defaulted
        on."""
        ctx, _ = _ctx(monkeypatch)
        body = await _body(
            ctx, {"prompt": "a red circle", "sequential_image_generation": "auto"}
        )
        assert body["sequential_image_generation"] == "auto"


@pytest.mark.parametrize("filename", ["images@v1.py"])
def test_every_version_is_within_the_script_sandbox(filename):
    """A script edit the loader would refuse must fail here, not on deploy."""
    from adapter.sandbox import scan_source

    scan_source(
        (ROOT / filename).read_text(encoding="utf-8"),
        filename=f"volcengine_ark/{filename}",
    )
