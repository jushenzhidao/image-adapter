"""Capability tables: loading, precedence, and the refusal to guess.

The point of this module is that measured facts live in data. These tests pin the
two things that would quietly ruin that: a loader that invents defaults when the
table is missing, and a precedence order that lets a stale image-store copy win
over an operator's hot-fixed overlay.
"""

from __future__ import annotations

import json

import pytest

from adapter.capabilities import clear_cache, known_models, lookup
from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.settings import Settings

REPO_ROOTS = (Settings().capability_roots[0],)


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each test starts from a cold parse cache (the module keeps one per worker)."""
    clear_cache()
    yield
    clear_cache()


def _table(tmp_path, vendor="google", payload=None):
    root = tmp_path / "capabilities"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{vendor}.json").write_text(
        json.dumps(payload if payload is not None else {"models": {}}), encoding="utf-8"
    )
    return root


# --- the shipped table ------------------------------------------------------


def test_the_shipped_table_answers_for_every_documented_model():
    caps = lookup("google", "gemini-3.1-flash-image", REPO_ROOTS)
    assert caps is not None
    assert caps["tiers"] == ["512", "1K", "2K", "4K"]
    assert caps["wide"] is True
    assert caps["kind"] == "generate"


def test_an_empty_tier_list_means_the_model_fixes_its_own_resolution():
    caps = lookup("google", "gemini-2.5-flash-image", REPO_ROOTS)
    assert caps is not None and caps["tiers"] == []


def test_aliases_resolve_to_the_base_model():
    caps = lookup("google", "nano-banana-pro", REPO_ROOTS)
    assert caps is not None
    assert caps["model"] == "gemini-3-pro-image"


def test_a_gateway_suffix_still_finds_the_base_model():
    """The regression that motivated the table: '-preview' used to miss entirely
    and silently fall back to another model's capabilities."""
    caps = lookup("google", "gemini-3.1-flash-image-preview", REPO_ROOTS)
    assert caps is not None
    assert caps["model"] == "gemini-3.1-flash-image"
    assert caps["tiers"] == ["512", "1K", "2K", "4K"]


def test_no_model_falls_back_to_the_declared_default():
    caps = lookup("google", None, REPO_ROOTS)
    assert caps is not None and caps["model"] == "gemini-3-pro-image"


def test_an_unknown_model_is_not_guessed_at():
    assert lookup("google", "gemini-404-nope", REPO_ROOTS) is None


def test_an_unknown_vendor_has_no_facts():
    assert lookup("nobody", "anything", REPO_ROOTS) is None


def test_known_models_lists_the_shipped_ones():
    assert known_models("google", REPO_ROOTS) == (
        "gemini-2.5-flash-image",
        "gemini-3-pro-image",
        "gemini-3.1-flash-image",
        "gemini-3.1-flash-lite-image",
    )


# --- missing / broken tables are "no facts", never an error -----------------


def test_a_missing_table_yields_nothing_rather_than_defaults():
    assert lookup("google", "gemini-3-pro-image", ()) is None


def test_a_malformed_table_is_ignored_not_raised(tmp_path):
    root = tmp_path / "capabilities"
    root.mkdir()
    (root / "broken.json").write_text("{ not json", encoding="utf-8")
    assert lookup("broken", "any", (root,)) is None


def test_a_non_object_table_is_ignored(tmp_path):
    root = _table(tmp_path, "listy", payload=[])
    assert lookup("listy", "any", (root,)) is None


def test_a_table_without_models_is_ignored(tmp_path):
    root = _table(tmp_path, "nomodels", payload={"vendor": "nomodels"})
    assert lookup("nomodels", "any", (root,)) is None


# --- precedence and caching -------------------------------------------------


def test_overlay_roots_win_over_the_image_store(tmp_path):
    overlay = _table(tmp_path, payload={
        "default_model": "gemini-3-pro-image",
        "models": {"gemini-3-pro-image": {"tiers": ["8K"], "wide": True}},
    })
    caps = lookup("google", "gemini-3-pro-image", (overlay, *REPO_ROOTS))
    assert caps is not None
    assert caps["tiers"] == ["8K"]           # the overlay, not the shipped 1K/2K/4K


def test_a_later_root_is_used_when_the_first_has_no_file(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    caps = lookup("google", "gemini-3-pro-image", (empty, *REPO_ROOTS))
    assert caps is not None and caps["tiers"] == ["1K", "2K", "4K"]


def test_an_edited_table_is_reread_without_a_restart(tmp_path):
    root = _table(tmp_path, payload={
        "models": {"m": {"tiers": ["1K"]}},
    })
    assert lookup("google", "m", (root,))["tiers"] == ["1K"]

    (root / "google.json").write_text(
        json.dumps({"models": {"m": {"tiers": ["1K", "2K", "4K"]}}}), encoding="utf-8"
    )
    assert lookup("google", "m", (root,))["tiers"] == ["1K", "2K", "4K"]


# --- through ctx ------------------------------------------------------------


def test_ctx_caps_reads_the_configured_roots():
    """The only path a script has to these facts."""
    settings = Settings(redis_url="", minio_endpoint="")
    ctx = AdapterContext(
        "req-1", ChannelSpec(upstream_url="https://vendor.test/api"), settings
    )
    caps = ctx.caps("google", "nano-banana-2")
    assert caps is not None
    assert caps["model"] == "gemini-3.1-flash-image"
    assert ctx.caps("google", "nope") is None
