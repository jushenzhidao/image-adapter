"""volcengine_ark/images: which wire form the channel's mode selects.

The failure these tests exist for is not a crash. ARK fetches a client-supplied
URL itself, under a hard 5 s cap on its side that no request parameter can
raise, and a request that trips it **cannot be retried**: the engine makes one
upstream call per request and raises on the non-2xx reply before the response
phase runs. So the wire form has to be right *before* the call.

The bug pinned here is that `image_ref_mode` used to be read only after the URL
shape had already been recognised and returned verbatim. The option was
therefore unable to prevent the one failure on this channel that nothing
downstream can repair -- the operator sets it, and still gets the timeout.

These are dispatch tests: they assert which conversion the script asks ctx for,
against the real ctx mixins (only the network and the object store are faked),
because the conversions themselves are covered by tests/unit/test_image_ref.py.

The fix ships as `images@v2`, so both versions are loaded. `v2` is where the
behaviour is now specified; `v1` is here only to prove that a channel pinned to
it keeps the old ordering, which is the whole reason this is a version rather
than an edit (see TestVersionSplit).
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
from typing import ClassVar

import pytest

from adapter.context import AdapterContext
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "volcengine_ark"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ark = _load("ark_images_v2", "images@v2.py")
ark_v1 = _load("ark_images_v1", "images@v1.py")

PNG = b"\x89PNG\r\n\x1a\n" + b"pretend payload bytes"
BARE = base64.b64encode(PNG).decode()
DATA_URI = f"data:image/png;base64,{BARE}"

#: A stand-in for the case that motivated the change: a source ARK cannot pull
#: inside its own 5 s budget.
URL = "https://overseas.example/holiday.png"

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
    script quietly made the wrong call.
    """

    def __init__(self) -> None:
        self.downloads: list[str] = []
        self.uploads: list[bytes] = []


def _ctx(monkeypatch, **options) -> tuple[AdapterContext, Spy]:
    ctx = AdapterContext(
        request_id="req-1",
        channel=_ChannelStub(options),
        settings=Settings(minio_endpoint=""),
    )
    spy = Spy()

    async def fake_download(url: str) -> bytes:
        spy.downloads.append(url)
        return PNG

    async def fake_upload(data: bytes, ext: str = "png") -> str:
        spy.uploads.append(data)
        return REHOSTED

    monkeypatch.setattr(ctx, "download_image", fake_download)
    monkeypatch.setattr(ctx, "upload_temp_image", fake_upload)
    return ctx, spy


async def _image_field(ctx, image):
    body = await ark.transform(
        ctx, {"prompt": "replace the circle", "image": image}, "request"
    )
    return body["image"]


class TestDefaultMode:
    """`url` mode: unchanged, and the reason it must stay unchanged."""

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
    """The fix: the mode is honoured for URLs too, so ARK fetches nothing."""

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
        exact defect this file is about.
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


@pytest.mark.asyncio
async def test_an_unknown_mode_still_falls_through_to_url(monkeypatch):
    """Pinned deliberately: this is the old behaviour, not a new failure mode.

    A channel carrying a value this script does not know must keep working
    exactly as it did rather than start refusing requests.
    """
    ctx, spy = _ctx(monkeypatch, image_ref_mode="something-else")
    assert await _image_field(ctx, URL) == URL
    assert spy.downloads == []


class TestVersionSplit:
    """`@v1` keeps the ordering, and `@v2` is otherwise a safe promotion.

    These two tests are the reason the change ships as a new version: the
    promotion has to be safe for every channel that does not opt in, and
    reversible for one that needs the old behaviour.
    """

    @pytest.mark.asyncio
    async def test_v1_still_hands_ark_the_url_even_in_data_uri_mode(
        self, monkeypatch
    ):
        """The pre-fix behaviour, kept reachable by pinning `@v1`."""
        ctx, spy = _ctx(monkeypatch, image_ref_mode="data_uri")
        body = await ark_v1.transform(
            ctx, {"prompt": "replace the circle", "image": URL}, "request"
        )
        assert body["image"] == URL
        assert spy.downloads == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("module", [ark_v1, ark], ids=["v1", "v2"])
    async def test_the_default_mode_is_identical_on_both_versions(
        self, monkeypatch, module
    ):
        """No mode set means no behaviour change, which is what makes @v2 safe.

        A channel that never sets `image_ref_mode` cannot tell the two versions
        apart by anything these tests can observe -- zero downloads either way.
        """
        ctx, spy = _ctx(monkeypatch)
        body = await module.transform(
            ctx, {"prompt": "replace the circle", "image": URL}, "request"
        )
        assert body["image"] == URL
        assert spy.downloads == []
        assert spy.uploads == []


@pytest.mark.parametrize("filename", ["images@v1.py", "images@v2.py"])
def test_both_versions_are_within_the_script_sandbox(filename):
    """A script edit the loader would refuse must fail here, not on deploy."""
    from adapter.sandbox import scan_source

    scan_source(
        (ROOT / filename).read_text(encoding="utf-8"),
        filename=f"volcengine_ark/{filename}",
    )
