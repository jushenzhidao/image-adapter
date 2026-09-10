"""Composition guarantees for the mixin-assembled ctx.

These are structural tests. Behaviour of each helper is covered by the image,
stage and endpoint suites; what is asserted here is that the assembly itself
holds, because a broken MRO or a mixin that grows an __init__ would fail in
subtle, per-method ways rather than loudly.
"""

import inspect

import pytest

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext, ContextCore
from adapter.ctxapi import CTX_MIXINS
from adapter.settings import Settings

# Every name a script is allowed to reach on ctx. Its purpose is to fail when
# the flat surface silently changes shape during a refactor.
SCRIPT_API = (
    "encode_b64",
    "decode_b64",
    "data_uri",
    "is_url",
    "is_data_uri",
    "sniff_mime",
    "download_image",
    "upload_temp_image",
    "image_bytes",
    "image_b64",
    "image_data_uri",
    "image_url",
    "emit",
    "sleep",
    "image",
    "remaining",
    "deadline",
    "SKIP",
)


def _ctx() -> AdapterContext:
    settings = Settings(redis_url="", minio_endpoint="")
    channel = ChannelSpec(upstream_url="https://vendor.test/api")
    return AdapterContext("req-1", channel, settings, endpoint="images")


@pytest.mark.parametrize("name", SCRIPT_API)
def test_script_api_is_flat_on_the_context(name):
    assert hasattr(_ctx(), name), f"ctx.{name} disappeared from the composed API"


def test_registry_matches_the_declared_bases():
    """CTX_MIXINS is documentation; this keeps it honest against the real MRO."""
    mro = AdapterContext.__mro__
    for mixin in CTX_MIXINS:
        assert mixin in mro, f"{mixin.__name__} is registered but not composed"


def test_core_is_last_so_mixins_cannot_shadow_infra():
    mro = AdapterContext.__mro__
    core = mro.index(ContextCore)
    assert all(mro.index(m) < core for m in CTX_MIXINS)


@pytest.mark.parametrize("mixin", CTX_MIXINS, ids=lambda m: m.__name__)
def test_mixins_define_no_init(mixin):
    """A mixin with its own __init__ would make base order load-bearing."""
    assert "__init__" not in mixin.__dict__


def test_mixins_do_not_collide_on_method_names():
    seen: dict[str, str] = {}
    for mixin in CTX_MIXINS:
        for name, value in vars(mixin).items():
            if name.startswith("_") or not callable(value):
                continue
            assert name not in seen, (
                f"{name} defined by both {seen[name]} and {mixin.__name__}"
            )
            seen[name] = mixin.__name__


def test_budget_and_plan_helpers_replace_private_pokes():
    """The engine steers ctx through methods, not private attributes."""
    ctx = _ctx()
    assert ctx.remaining is None and ctx.deadline is None

    ctx.emit(method="post", query={"a": "1"})
    assert ctx.plan.method == "POST"
    ctx.reset_plan()
    assert ctx.plan.method is None and ctx.plan.query == {}

    assert inspect.ismethod(ctx.attach_budget)
