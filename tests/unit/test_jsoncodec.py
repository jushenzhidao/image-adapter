"""The shared JSON codec's contract.

``adapter.jsoncodec`` is the single place a body is encoded or decoded, so the
two behaviours that differ from Starlette's ``JSONResponse`` are pinned here --
a later "cleanup" back to the stdlib would otherwise pass every other test:

  * an integer dict key serialises as a string, exactly as the stdlib does.
    Without ``OPT_NON_STR_KEYS`` orjson raises TypeError, which would turn a
    perfectly good response into a 500.
  * ``NaN``/``Infinity`` serialise as ``null``, where the stdlib (with
    ``allow_nan=False``) raises. A vendor emitting a bare ``Infinity`` costs
    the caller one field, not the whole response.
"""

from __future__ import annotations

import json

from adapter import jsoncodec


def test_loads_accepts_bytes_without_a_prior_decode():
    assert jsoncodec.loads(b'{"a":1}') == {"a": 1}


def test_dumps_leaves_non_ascii_literal():
    body = jsoncodec.dumps({"prompt": "一只猫"})
    assert "一只猫".encode() in body
    assert b"\\u" not in body


def test_empty_containers_and_none_survive():
    assert json.loads(jsoncodec.dumps({"a": [], "b": {}, "c": None})) == {
        "a": [],
        "b": {},
        "c": None,
    }


def test_int_dict_keys_become_strings_like_the_stdlib():
    assert json.loads(jsoncodec.dumps({1: "a"})) == {"1": "a"}


def test_nan_and_infinity_degrade_to_null():
    body = jsoncodec.dumps({"nan": float("nan"), "inf": float("inf")})
    assert json.loads(body) == {"nan": None, "inf": None}


def test_response_class_renders_with_the_shared_encoder():
    resp = jsoncodec.JSONResponse({"ok": True})
    assert resp.media_type == "application/json"
    assert resp.body == b'{"ok":true}'


def test_response_class_keeps_status_and_headers():
    resp = jsoncodec.JSONResponse({"e": 1}, status_code=429, headers={"X-A": "b"})
    assert resp.status_code == 429
    assert resp.headers["X-A"] == "b"
