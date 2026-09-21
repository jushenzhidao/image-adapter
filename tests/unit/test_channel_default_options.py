"""``DEFAULT_CHANNEL_OPTIONS``: deployment-wide defaults merged under a channel.

The feature is small, and what is worth pinning is the part every later change
will be tempted to relax:

  * an empty value is a **no-op**, byte for byte -- the knob must be safe to
    leave unset and safe to roll back;
  * the *channel header wins* on a shared key: a channel can always override
    the deployment's value, so the default can never pin one channel down;
  * the merge is shallow and additive: keys only one side carries both survive;
  * a malformed value is refused **by name** (``parse_default_options``), which
    is what lets the startup check in ``main.py`` be a boot failure rather than
    a 400 the first request discovers.
"""

from __future__ import annotations

import pytest

from adapter.channel import parse_channel, parse_default_options
from adapter.errors import ChannelConfigError
from adapter.settings import Settings

#: A minimal usable channel, lowercase because `parse_channel` reads raw header
#: names (see tests/unit/test_model_map.py for the same convention).
BASE = {
    "x-upstream-url": "https://api.vendor.test/v1/images/generations",
    "x-script-ref": "openai/images@v1",
}


def _settings(**overrides) -> Settings:
    # `_env_file=None`: a bare Settings() reads the production .env, which would
    # make this suite's outcome depend on the machine it runs on.
    return Settings(_env_file=None, adapter_key_required=False, **overrides)


def test_empty_default_keeps_channel_options_as_is():
    """不配这个旋钮时，options 与头里写的逐键一致（零影响）。"""
    spec = parse_channel(
        {**BASE, "x-channel-options": '{"identity_url": "http://only/header"}'},
        _settings(),
    )
    assert spec.options == {"identity_url": "http://only/header"}


def test_default_sits_under_header_options():
    """默认垫底：头不写 → 脚本仍拿到默认键；头写别的 → 两边并存。"""
    spec = parse_channel(
        {**BASE, "x-channel-options": '{"image_model": "qwen-image-3.0"}'},
        _settings(
            default_channel_options='{"identity_url": "http://svc:8791/identity"}'
        ),
    )
    assert spec.options == {
        "identity_url": "http://svc:8791/identity",
        "image_model": "qwen-image-3.0",
    }


def test_default_applies_when_the_header_is_absent_entirely():
    """头完全不发（未来渠道不用写那行）时，默认键仍在。"""
    spec = parse_channel(BASE, _settings(default_channel_options='{"a": 1}'))
    assert spec.options == {"a": 1}


def test_header_wins_on_shared_key():
    """同名键头赢：默认永远压不住某个渠道的显式声明。"""
    spec = parse_channel(
        {**BASE, "x-channel-options": '{"identity_url": "http://channel/own"}'},
        _settings(
            default_channel_options='{"identity_url": "http://deployment/default"}'
        ),
    )
    assert spec.options == {"identity_url": "http://channel/own"}


def test_malformed_default_is_refused_by_name():
    """坏值按名拒绝——启动校验（main.py）靠的就是这个异常。"""
    for bad in ("not json", "[1, 2]", '"a string"'):
        with pytest.raises(ChannelConfigError) as excinfo:
            parse_default_options(bad)
        assert "DEFAULT_CHANNEL_OPTIONS" in str(excinfo.value)
    assert parse_default_options("") == {}
    assert parse_default_options('{"k": "v"}') == {"k": "v"}


def test_legacy_model_map_in_the_default_is_refused_too():
    """legacy 键检查发生在合并之后：默认里塞 model_map 同样 400，而非静默。"""
    with pytest.raises(ChannelConfigError):
        parse_channel(BASE, _settings(default_channel_options='{"model_map": "a=b"}'))
