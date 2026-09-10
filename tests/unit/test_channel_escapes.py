"""X-Script's escape grammar, pinned against the implementation it replaced.

The character loop was rewritten as a single regex pass because it measured
0.345 ms against 0.026 ms on an 8 KiB script, and it runs on the event loop
before any handler work starts. The only thing such a rewrite can get wrong is
the semantics -- which sequences count as escapes, what an unknown one does,
and what a trailing backslash does -- so the cases below are compared against
the old loop itself rather than against hand-written expectations.

Note on the literals here: these are the *header* strings, so ``"\\n"`` is the
two characters backslash + n, exactly as they travel on the wire.
"""

from __future__ import annotations

import pytest

from adapter.channel import _unescape_inline


def _loop(raw: str) -> str:
    """The implementation this one replaced, kept verbatim as the oracle."""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt == "n":
            out.append("\n")
        elif nxt == "t":
            out.append("\t")
        elif nxt == "r":
            out.append("\r")
        elif nxt == "\\":
            out.append("\\")
        else:
            out.append(ch)
            out.append(nxt)
        i += 2
    return "".join(out)


CASES = [
    "",
    "\\",
    "\\\\",
    "plain",
    "a\\nb",
    "a\\tb",
    "a\\rb",
    "a\\\\b",
    "a\\qb",  # undefined escape
    "trailing\\",
    "\\\\n",  # escaped backslash followed by a literal n
    "\\\\\\n",  # escaped backslash, then a real newline escape
    "\\n\\t\\r\\\\",
    "async def transform(ctx, payload, phase):\\n    return payload",
]


@pytest.mark.parametrize("raw", CASES)
def test_matches_the_loop_it_replaced(raw: str):
    assert _unescape_inline(raw) == _loop(raw)


def test_the_documented_escapes_do_what_the_contract_says():
    assert _unescape_inline("a\\nb") == "a\nb"
    assert _unescape_inline("a\\tb") == "a\tb"
    assert _unescape_inline("a\\rb") == "a\rb"
    assert _unescape_inline("a\\\\b") == "a\\b"


def test_an_undefined_escape_survives_as_two_characters():
    """Kept rather than dropped, so a script is never silently altered: a
    regex like ``re\\d+`` reaches the interpreter exactly as written."""
    assert _unescape_inline("re\\d+") == "re\\d+"


def test_a_lone_trailing_backslash_survives():
    """The dot does not match a newline and there is no following character at
    all, so the substitution leaves it alone."""
    assert _unescape_inline("x\\") == "x\\"


def test_a_backslashed_backslash_does_not_swallow_the_next_escape():
    """``\\\\n`` is a literal backslash followed by ``n``, not a newline."""
    assert _unescape_inline("\\\\n") == "\\n"
