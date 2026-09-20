"""The channel contract exists in three places, hand-kept in step.

  1. ``adapter/channel.py``'s ``H_*`` constants -- what the parser reads;
  2. ``adapter/main.py::channel_contract`` -- what /docs lists and FastAPI
     advertises;
  3. ``adapter/middleware/cors.py`` -- what a browser preflight will let
     through.

The CORS module says "kept in step with channel_contract" in a comment, which
is precisely the kind of agreement that rots without anyone noticing. The
failure is quiet by construction: a header missing from the allow-list is
refused by the *browser* before the request is sent, so it presents as "that
header does not work" rather than as a config mistake -- and a header added to
the parser but never declared stays invisible in /docs, which is the one place
callers look.

Adding ``X-Upstream-Proxy`` is what made this worth pinning: it is the first
header added to this contract in a while, and it needed edits in all three
files.
"""

from __future__ import annotations

import typing

from adapter import channel
from adapter.main import channel_contract
from adapter.middleware.cors import ALWAYS_ALLOWED_HEADERS, CHANNEL_HEADERS


def _parser_headers() -> set[str]:
    """Lowercased header names the channel parser actually reads."""
    return {
        value.lower()
        for name, value in vars(channel).items()
        if name.startswith("H_") and isinstance(value, str)
    }


def _declared_headers() -> set[str]:
    """Lowercased aliases declared on the FastAPI contract.

    Read off the annotation metadata by attribute rather than by isinstance:
    ``fastapi.Header`` is a factory function, not a type, so there is nothing
    to isinstance against -- and going through ``fastapi.params.Param`` would
    tie this test to an internal module for no gain.
    """
    declared: set[str] = set()
    hints = typing.get_type_hints(channel_contract, include_extras=True)
    for hint in hints.values():
        for meta in typing.get_args(hint)[1:]:
            alias = getattr(meta, "alias", None)
            if isinstance(alias, str):
                declared.add(alias.lower())
    return declared


def test_the_docs_contract_lists_exactly_the_headers_the_parser_reads() -> None:
    assert _declared_headers() == _parser_headers(), (
        "a channel header was added to (or removed from) one of the two lists: "
        "adapter/channel.py H_* and adapter/main.py::channel_contract"
    )


def test_every_channel_header_survives_a_browser_preflight() -> None:
    allowed = {h.lower() for h in (*CHANNEL_HEADERS, *ALWAYS_ALLOWED_HEADERS)}
    missing = sorted(_parser_headers() - allowed)
    assert not missing, (
        f"these channel headers are not in the CORS allow-list, so a browser "
        f"preflight refuses the whole request: {missing}"
    )
