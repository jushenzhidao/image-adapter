"""Pure mapping helpers: client intent -> the nearest thing a vendor supports.

Every image upstream speaks a different dialect for the same three questions --
"how big", "what shape", "how do I name this model" -- and each script used to
re-derive the answers. The derivations are small but easy to get subtly wrong, and
a wrong answer is silent: the request still succeeds, only the size, the price and
the tokens differ from what the caller asked for. One such mistake shipped and was
caught only by a unit test (a 512 request upgraded to 4K, quadrupling the tokens).

What lives here is the *logic*; the *facts* (which tiers a model has, which ratios
it accepts) come from ``ctx.caps()`` and are owned by ``capabilities/*.json``.

Two rules shape these helpers:

* **No guessing.** A helper that cannot answer returns None instead of a default,
  so the caller decides whether to send less or refuse. Fabricating a value is how
  a silent behaviour change gets introduced.
* **Direction is explicit.** ``clamp`` treats the request as a ceiling (never
  upgrade a caller who asked for something small), ``escalate`` treats it as a floor
  (the vendor refuses anything smaller). They are opposite operations and are not
  allowed to share a default.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from adapter.ctxapi.base import CtxMixin

#: Suffixes a tier name may carry, and what they mean. "2k" is the long edge in
#: pixels, which is how the whole industry spells these presets.
_TIER_SUFFIXES = (("px", 1), ("k", 1024), ("m", 1024 * 1024))

_Size = tuple[int, int]


def _as_pair(ratio: Any) -> _Size | None:
    """A ratio as (w, h), from either a pair or a "16:9" string."""
    if isinstance(ratio, str):
        head, sep, tail = ratio.partition(":")
        if not sep:
            return None
        ratio = (head, tail)
    if not isinstance(ratio, (tuple, list)) or len(ratio) != 2:
        return None
    try:
        w, h = (int(str(part).strip()) for part in ratio)
    except (TypeError, ValueError):
        return None
    return (w, h) if w > 0 and h > 0 else None


class MappingMixin(CtxMixin):
    """Size/shape arithmetic shared by every upstream script."""

    @staticmethod
    def tier_value(tier: Any) -> int | None:
        """``"512"``/``"1k"``/``"2k"``/``1536`` -> a comparable pixel count.

        Returns None for anything unparsable, including bools -- a caller that
        cannot compare two tiers must not pretend they are equal.
        """
        if isinstance(tier, bool):
            return None
        if isinstance(tier, int):
            return tier
        if not isinstance(tier, str):
            return None
        text = tier.strip().lower()
        if not text:
            return None
        multiplier = 1
        for suffix, factor in _TIER_SUFFIXES:
            if text.endswith(suffix):
                multiplier = factor
                text = text[: -len(suffix)]
                break
        try:
            return int(float(text) * multiplier)
        except ValueError:
            return None

    @staticmethod
    def size_to_px(size: Any) -> _Size | None:
        """OpenAI's ``"1024x1024"`` -> ``(1024, 1024)``, or None if unusable."""
        try:
            w, h = (int(part) for part in str(size).lower().split("x", 1))
        except (TypeError, ValueError):
            return None
        return (w, h) if w > 0 and h > 0 else None

    def fit_tier(
        self, want: int, tiers: Sequence[Any], *, policy: str = "clamp"
    ) -> tuple[Any, bool]:
        """The tier closest to ``want`` without crossing it, plus "was it moved".

        ``policy="clamp"``   -- ``want`` is a ceiling: pick the highest tier that is
                                not above it. Nothing smaller than every tier is
                                upgraded; the model's floor is returned instead.
                                (This is the one that used to round *up* to the
                                ceiling, turning a 512 request into 4K.)
        ``policy="escalate"``-- ``want`` is a floor: pick the lowest tier that
                                satisfies it, because the vendor refuses less
                                (ARK refuses anything under 3686400 pixels).

        The second element is True when the returned tier does not equal ``want``,
        so a caller can report the substitution instead of hiding it. Tier names
        that cannot be compared are ignored; if none can, the answer is (None, False).
        """
        # Only comparable tiers take part; anything the parser cannot read (a
        # placeholder like "auto", a None) is dropped rather than compared.
        ranked: list[tuple[int, Any]] = []
        for tier in tiers:
            value = self.tier_value(tier)
            if value is not None:
                ranked.append((value, tier))
        if not ranked:
            return None, False
        ranked.sort(key=lambda pair: pair[0])

        if policy == "escalate":
            candidates = [pair for pair in ranked if pair[0] >= want] or [ranked[-1]]
            chosen = candidates[0]
        else:  # clamp (default): never cross the ceiling
            candidates = [pair for pair in ranked if pair[0] <= want] or [ranked[0]]
            chosen = candidates[-1]
        return chosen[1], chosen[0] != want

    @staticmethod
    def fit_ratio(
        w: int, h: int, allowed: Iterable[Any]
    ) -> _Size | None:
        """The ratio in ``allowed`` closest to ``w:h``, or None if none usable.

        Distance is the plain difference of the two ratios, with a tie broken
        towards the squarer option -- so a 4:3 request lands on 1:1 rather than
        16:9 when both are equally far. Whether an extreme ratio is acceptable at
        all is the caller's business: filtering ``allowed`` first is how a model
        that refuses 1:8 is handled, and keeping that decision outside keeps this
        function about arithmetic only.
        """
        if w <= 0 or h <= 0:
            return None
        pairs = [pair for pair in (_as_pair(item) for item in allowed) if pair]
        if not pairs:
            return None
        target = w / h
        return min(pairs, key=lambda pair: (abs(pair[0] / pair[1] - target),
                                           abs(pair[0] / pair[1] - 1)))

    @staticmethod
    def format_ratio(pair: _Size | None) -> str | None:
        """``(16, 9)`` -> ``"16:9"``. Vendors spell ratios as strings, not tuples."""
        return f"{pair[0]}:{pair[1]}" if pair else None

    @staticmethod
    def is_extreme_ratio(pair: Any, *, fold: float = 4.0) -> bool:
        """True when a shape folds past ``fold`` (default 4x): 1:4, 8:1, ...

        Derived from the numbers rather than a hand-kept list of "extreme" ratios.
        The list existed as four hard-coded pairs, and the next vendor would have
        needed its own; folding is what makes 21:9 ordinary (2.33x) and 1:8 extreme
        (8x), which is the actual distinction callers care about.
        """
        parsed = _as_pair(pair)
        if not parsed:
            return False
        ratio = parsed[0] / parsed[1]
        return max(ratio, 1 / ratio) >= fold
