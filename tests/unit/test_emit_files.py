"""``ctx.emit(files=...)`` and the multipart it produces.

The primitive exists because an OpenAI-native edits endpoint takes an image
as a file part, which a JSON body cannot express. Two things need pinning: the
normalisation that lets a script pass one part or several, and the fact that a
plan carrying files makes ``build_request`` assemble multipart instead of JSON
-- with no stale Content-Type left to strip the boundary off.
"""

from __future__ import annotations

import aiohttp
import pytest

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.ctxapi import RequestPlan
from adapter.settings import Settings
from adapter.transport import build_request

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 4


def _ctx() -> AdapterContext:
    settings = Settings(redis_url="", minio_endpoint="")
    channel = ChannelSpec(upstream_url="https://api.test/v1/images/edits")
    return AdapterContext("req-1", channel, settings, endpoint="images")


def _channel() -> ChannelSpec:
    return ChannelSpec(upstream_url="https://api.test/v1/images/edits")


# --- normalisation --------------------------------------------------------


def test_a_single_part_is_wrapped_into_a_list():
    """The common case gets the ergonomic spelling, stored canonically."""
    ctx = _ctx()
    ctx.emit(files={"image": ("a.png", PNG, "image/png")})
    assert ctx.plan.files == {"image": [("a.png", PNG, "image/png")]}


def test_several_parts_for_one_field_keep_their_order():
    """A repeated field is how a multi-image edit is spelled."""
    ctx = _ctx()
    ctx.emit(files={"image[]": [("a.png", PNG, "image/png"), ("b.png", GIF, "image/gif")]})
    assert ctx.plan.files == {
        "image[]": [("a.png", PNG, "image/png"), ("b.png", GIF, "image/gif")]
    }


def test_bytearray_and_memoryview_parts_are_accepted():
    ctx = _ctx()
    ctx.emit(files={"image": ("a.png", bytearray(PNG), "image/png")})
    assert ctx.plan.files["image"][0][1] == PNG


@pytest.mark.parametrize(
    "part, message",
    [
        (("a.png", "not bytes", "image/png"), "must be bytes"),
        (("a.png", PNG), "parts must be"),
    ],
)
def test_a_malformed_part_fails_with_a_clear_message(part, message):
    """A script bug should read as a script bug, not as an aiohttp error."""
    ctx = _ctx()
    with pytest.raises(TypeError, match=message):
        ctx.emit(files={"image": part})


def test_no_files_leaves_the_plan_alone():
    ctx = _ctx()
    ctx.emit(files=None)
    assert ctx.plan.files is None
    ctx.emit(body={"a": 1})
    assert ctx.plan.files is None and ctx.plan.body == {"a": 1}


# --- transport selection --------------------------------------------------


def test_files_switch_the_call_to_multipart():
    plan = RequestPlan(
        files={"image": [("a.png", PNG, "image/png")]},
        # A Content-Type that arrived earlier (an auth phase, say) must not
        # survive: a boundary-less multipart header makes the body unparseable.
        headers={"Content-Type": "application/json"},
    )
    _, method, kwargs = build_request(_channel(), plan, {"prompt": "x"}, None)

    assert method == "POST"
    assert isinstance(kwargs["data"], aiohttp.FormData)
    assert kwargs["data"].is_multipart
    assert "json" not in kwargs
    assert not any(k.lower() == "content-type" for k in kwargs["headers"])


def test_without_files_the_body_stays_json():
    _, _, kwargs = build_request(_channel(), RequestPlan(), {"prompt": "x"}, None)
    assert kwargs["json"] == {"prompt": "x"}
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_raw_still_wins_over_files():
    plan = RequestPlan(raw=b"already-encoded", files={"image": [("a.png", PNG, "image/png")]})
    _, _, kwargs = build_request(_channel(), plan, {"prompt": "x"}, None)
    assert kwargs["data"] == b"already-encoded"


def test_files_win_over_form_encoding():
    plan = RequestPlan(
        form={"legacy": "1"}, files={"image": [("a.png", PNG, "image/png")]}
    )
    _, _, kwargs = build_request(_channel(), plan, {"prompt": "x"}, None)
    assert isinstance(kwargs["data"], aiohttp.FormData)
