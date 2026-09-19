"""Mapping arithmetic: the rules that decide size, tier and aspect ratio.

These were extracted from a channel script after one of them shipped a silent
mistake: a 512 request came back as 4K because the "no tier at or below what was
asked" case rounded *up* to the ceiling. The cases below are mostly that shape --
boundaries where a wrong answer still returns 200, just not what was asked for.
"""

from __future__ import annotations

import pytest

from adapter.ctxapi.mapping import MappingMixin

m = MappingMixin()


# --- tier_value -------------------------------------------------------------


@pytest.mark.parametrize(
    "tier, expected",
    [
        (512, 512),
        (1536, 1536),
        ("512", 512),
        ("1k", 1024),
        ("1K", 1024),
        ("2K", 2048),
        ("4k", 4096),
        ("1.5K", 1536),
        ("2M", 2 * 1024 * 1024),
        ("768px", 768),
        ("  2k  ", 2048),
        ("", None),
        ("k", None),
        ("abc", None),
        (None, None),
        (3.5, None),
        (True, None),        # bool is an int subclass; comparing it would be a lie
        (False, None),
    ],
)
def test_tier_value_reads_the_size_a_vendor_means(tier, expected):
    assert m.tier_value(tier) == expected


# --- size_to_px -------------------------------------------------------------


@pytest.mark.parametrize(
    "size, expected",
    [
        ("1024x1024", (1024, 1024)),
        ("512X512", (512, 512)),
        ("1920x1080", (1920, 1080)),
        ("garbage", None),
        ("0x100", None),
        ("-4x8", None),
        (None, None),
        (1024, None),
    ],
)
def test_size_to_px_is_strict_about_junk(size, expected):
    assert m.size_to_px(size) == expected


# --- fit_tier, clamp (the direction that shipped a bug) --------------------

TIERS = ("1K", "2K", "4K")


@pytest.mark.parametrize(
    "want, expected",
    [
        (1024, ("1K", False)),
        (4096, ("4K", False)),
        (3000, ("2K", True)),
        # The regression: a request below every tier must land on the floor,
        # never on the ceiling. 512 used to become 4K -- right size wrong by 8x,
        # and roughly 4x the tokens.
        (512, ("1K", True)),
        (1, ("1K", True)),
    ],
)
def test_clamp_never_crosses_the_ceiling(want, expected):
    assert m.fit_tier(want, TIERS) == expected


def test_clamp_places_a_request_between_two_tiers_below_it():
    tiers = ("512", "1K", "2K", "4K")
    assert m.fit_tier(3000, tiers) == ("2K", True)
    assert m.fit_tier(2000, tiers) == ("1K", True)
    assert m.fit_tier(2048, tiers) == ("2K", False)


# --- fit_tier, escalate ----------------------------------------------------


@pytest.mark.parametrize(
    "want, expected",
    [
        (1920, ("2k", True)),          # 2k is the first tier that satisfies it
        (2048, ("2k", False)),
        (2049, ("4k", True)),
        (99999, ("4k", True)),         # nothing satisfies it: take the largest
    ],
)
def test_escalate_never_goes_below_the_floor(want, expected):
    """want and the tiers must share a unit -- 3686400 pixels is not 3686400
    of long edge, and mixing them silently picks the wrong rung."""
    assert m.fit_tier(want, ("2k", "4k"), policy="escalate") == expected


# --- fit_tier, degenerate inputs -------------------------------------------


def test_uncomparable_tiers_are_ignored_rather_than_compared():
    assert m.fit_tier(2048, ("1K", "auto", "4K")) == ("1K", True)
    assert m.fit_tier(4096, ("1K", None, "4K")) == ("4K", False)


def test_no_comparable_tier_yields_nothing_rather_than_a_guess():
    assert m.fit_tier(1024, ("auto", "native")) == (None, False)
    assert m.fit_tier(1024, ()) == (None, False)


def test_an_unknown_policy_behaves_like_the_safe_default():
    """Only "escalate" opts into upgrading; anything else must not upgrade."""
    assert m.fit_tier(512, TIERS, policy="whatever") == ("1K", True)


# --- fit_ratio --------------------------------------------------------------


@pytest.mark.parametrize(
    "w, h, allowed, expected",
    [
        (1024, 1024, [(1, 1), (16, 9)], (1, 1)),
        (1920, 1080, ["1:1", "16:9"], (16, 9)),      # string form is accepted
        (1080, 1920, ["1:1", "9:16"], (9, 16)),
        (1024, 1024, [(1, 4), (4, 1)], (1, 4)),
        # 5:4 is 1.25, exactly between 2:1 (2.0) and 1:2 (0.5): a true tie, so the
        # squarer candidate wins. (Both values are exactly representable in binary --
        # a tie built from 4:3 and 3:4 is not a tie at all once floats round.)
        (5, 4, [(2, 1), (1, 2)], (1, 2)),
    ],
)
def test_fit_ratio_picks_the_nearest_documented_shape(w, h, allowed, expected):
    assert m.fit_ratio(w, h, allowed) == expected


def test_fit_ratio_says_nothing_when_it_cannot_answer():
    assert m.fit_ratio(1024, 1024, []) is None
    assert m.fit_ratio(1024, 1024, ["not-a-ratio", None]) is None
    assert m.fit_ratio(0, 0, [(1, 1)]) is None


def test_fit_ratio_tolerates_a_single_candidate():
    assert m.fit_ratio(1920, 1080, [(1, 1)]) == (1, 1)


# --- format_ratio ----------------------------------------------------------


def test_format_ratio_spells_it_the_way_vendors_do():
    assert m.format_ratio((16, 9)) == "16:9"
    assert m.format_ratio(None) is None


# --- composed by the caller ------------------------------------------------


def test_a_model_that_fixes_its_resolution_is_told_apart_from_a_bad_request():
    """A model with no tiers is a different thing from a request we cannot read,
    and both are different from "mapped to something else"."""
    assert m.fit_tier(1024, ()) == (None, False)          # no tiers at all
    assert m.fit_tier(1024, TIERS) == ("1K", False)       # exact match
    assert m.fit_tier(2048, TIERS) == ("2K", False)
    assert m.fit_tier(2500, TIERS) == ("2K", True)        # substituted


# --- parse_size：方言识别与归一化（出线形态仍归调用方） ---------------------

#: 一个厂商的方言事实（qwen 的形状）：比例枚举 -> 规范分辨率、档位拼法。
RATIOS = {"1:1": "2048*2048", "16:9": "2688*1536", "9:16": "1536*2688"}
QTiers = ("1K", "2K", "4K")


def test_parse_size_reads_a_ratio_and_hands_back_its_resolution():
    r = m.parse_size("16:9", ratios=RATIOS, tiers=QTiers)
    assert (r.kind, r.ratio, r.hw) == ("ratio", "16:9", "2688*1536")
    assert r.exact is False            # 像素来自表，不是调用方给的


def test_parse_size_recognizes_both_resolution_separators():
    """`2688x1536` 与 `2688*1536` 是同一件事，且**就是**那个比例。"""
    for raw in ("2688*1536", "2688x1536", "2688X1536"):
        r = m.parse_size(raw, ratios=RATIOS, tiers=QTiers)
        assert (r.kind, r.ratio, r.hw, r.exact) == (
            "resolution", "16:9", "2688*1536", False), raw
        assert "16:9" in " ".join(r.notes)   # 说明为什么换了个写法


def test_parse_size_keeps_pixels_it_cannot_place():
    """非规范分辨率原样保留并标 exact：这是调用方自己的数字，不许塑形。"""
    r = m.parse_size("1024*1024", ratios=RATIOS, tiers=QTiers)
    assert (r.kind, r.ratio, r.hw, r.exact) == ("resolution", None, "1024*1024", True)


def test_parse_size_reads_tiers_case_insensitively_and_keeps_the_spelling():
    r = m.parse_size("2k", ratios=RATIOS, tiers=QTiers)
    assert (r.kind, r.tier) == ("tier", "2K")     # 回的是厂商表里的拼法
    # 档位不能同时被读成分辨率或比例
    assert r.ratio is None and r.hw is None


def test_parse_size_never_reads_a_tier_as_a_resolution():
    """`1K` 里的 K 不是分隔符；只有 `NxM` 才算分辨率。"""
    r = m.parse_size("1024", ratios=RATIOS, tiers=())
    assert r.kind == "unknown"


def test_parse_size_reports_what_it_cannot_read_instead_of_guessing():
    assert m.parse_size("not-a-size", ratios=RATIOS, tiers=QTiers).kind == "unknown"
    assert m.parse_size("", ratios=RATIOS).kind == "auto"
    assert m.parse_size("  auto  ", ratios=RATIOS).kind == "auto"
    assert m.parse_size(None, ratios=RATIOS).kind == "auto"


def test_parse_size_honours_a_vendor_that_takes_no_absolute_resolution():
    """`resolutions=False`（如只认 aspectRatio 的厂商）⇒ W*H 就是读不懂。"""
    assert m.parse_size("1024x1024", ratios=RATIOS, resolutions=False).kind == "unknown"
    # 但比例枚举照样认得
    assert m.parse_size("1:1", ratios=RATIOS, resolutions=False).ratio == "1:1"


def test_parse_size_without_facts_reads_nothing():
    """没有厂商事实时不得凭空造值：比例与档位都无从判定。"""
    assert m.parse_size("16:9").kind == "unknown"
    assert m.parse_size("2K").kind == "unknown"
