"""Header suppression: an empty value means "do not send this header".

The convention exists so a *script* can keep something off a vendor that
refuses it. The qwen script suppresses the engine's default
``Authorization: Bearer <key>`` emission that way -- measured 2026-09-22,
that header alone drew x5sec/RGV587 on every write, and baking the
suppression into the script is what keeps every qwen channel free of
``X-Auth-Emit`` while the fleet-wide bearer default stays untouched.

Four assertions matter: the empty header disappears, it wins over the
credential emission (``apply_auth`` setdefaults), a channel that asks for
nothing still gets its bearer, and ``X-Auth-Emit: none`` keeps working.
"""

from __future__ import annotations

from adapter.channel import AuthEmit, ChannelSpec
from adapter.ctxapi import RequestPlan
from adapter.transport import build_request


def _channel(**kw) -> ChannelSpec:
    return ChannelSpec(
        upstream_url="https://api.test/v1/images/generations",
        upstream_key="sk-vendor",
        **kw,
    )


def test_an_empty_valued_header_is_not_sent():
    plan = RequestPlan(headers={"X-Trace": "", "X-Keep": "1"})
    _, _, kwargs = build_request(_channel(), plan, {"prompt": "x"}, None)
    assert "X-Trace" not in kwargs["headers"]
    assert kwargs["headers"]["X-Keep"] == "1"


def test_an_empty_authorization_suppresses_the_credential_emission():
    """The script's key wins at setdefault; the drop then removes it."""
    plan = RequestPlan(headers={"Authorization": ""})
    _, _, kwargs = build_request(_channel(), plan, {"prompt": "x"}, None)
    assert "Authorization" not in kwargs["headers"]


def test_a_channel_that_asks_for_nothing_still_gets_its_bearer():
    """The fleet-wide default: every upstream that wants bearer keeps it."""
    _, _, kwargs = build_request(_channel(), RequestPlan(), {"prompt": "x"}, None)
    assert kwargs["headers"]["Authorization"] == "Bearer sk-vendor"


def test_x_auth_emit_none_still_works():
    """The explicit knob stays honoured -- it is redundant for qwen, not gone."""
    channel = _channel(auth=AuthEmit(target="none", name="", prefix=""))
    _, _, kwargs = build_request(channel, RequestPlan(), {"prompt": "x"}, None)
    assert "Authorization" not in kwargs["headers"]
