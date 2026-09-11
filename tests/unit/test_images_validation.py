"""``validate_images_body`` normalises what it can instead of refusing it.

Three shapes mean "the client had nothing to say": an unset image arrives as
``null`` or as ``[]`` depending on the SDK, an unusable ``n`` arrives as
whatever the caller put there, and an unusable ``response_format`` -- ``null``,
``""``, ``"webp"``, a chat-shaped object -- arrives as whatever enum the caller
was working from. All three are normalised: the key is dropped, or ``n`` becomes
one. The alternative costs the caller its answer over a field the adapter was
never going to honour literally anyway.

Dropping the key is not the same as tolerating it. ``openai/images@v1`` forwards
the canonical body verbatim on its text-to-image path, so a surviving
``image: []`` would reach a vendor that refuses the argument outright: a request
this adapter accepted would come back as an upstream error.

What must *not* become permissive is pinned here too.
"""

from __future__ import annotations

import pytest

from adapter.api.images import validate_images_body
from adapter.errors import InvalidRequestError


@pytest.mark.parametrize("unset", [None, []], ids=["null", "empty-list"])
def test_an_unset_image_is_dropped_rather_than_refused(unset):
    body = {"prompt": "cat", "image": unset}
    validate_images_body(body)
    assert "image" not in body


def test_an_unset_image_still_needs_a_prompt():
    """Text-to-image and "nothing at all" must stay distinguishable."""
    with pytest.raises(InvalidRequestError) as caught:
        validate_images_body({"image": []})
    assert caught.value.param == "prompt"


@pytest.mark.parametrize(
    "bad", [0, -1, "two", True, None, 2.5], ids=["zero", "negative", "text",
                                                 "bool", "null", "float"]
)
def test_an_unusable_n_falls_back_to_one(bad):
    body = {"prompt": "cat", "n": bad}
    validate_images_body(body)
    assert body["n"] == 1


def test_a_valid_n_is_left_alone():
    body = {"prompt": "cat", "n": 2}
    validate_images_body(body)
    assert body["n"] == 2


def test_an_absent_n_is_not_invented():
    """Normalising a bad value must not start sending a field nobody asked for."""
    body = {"prompt": "cat"}
    validate_images_body(body)
    assert "n" not in body


def test_a_blank_image_is_still_refused():
    """`""` is a client bug, not an unset field, so it stays an error."""
    with pytest.raises(InvalidRequestError) as caught:
        validate_images_body({"image": ""})
    assert caught.value.param == "image"


@pytest.mark.parametrize(
    "bad",
    ["hologram", "b64", "", None, 1, ["url"], {"type": "json_object"}],
    ids=["junk", "abbrev", "blank", "null", "int", "list", "chat-shaped"],
)
def test_an_unusable_response_format_falls_back_to_unspecified(bad):
    """Junk is silence, not an error: the key goes, the picture stands.

    Every script already reads the field this way (`openai/images@v1::
    _requested_format` maps anything outside its carrier set to None), so
    refusing it here made this door stricter than the components that act on
    it. Dropped rather than blanked because `openai/images@v1` forwards the
    canonical body verbatim on its text-to-image path.

    `None` is in the list on purpose: several SDKs serialise an unset enum as
    null, which is exactly the "the client said nothing" case -- and leaving a
    null in the body would be forwarded as the argument `null` by a script whose
    default lives in `.get(key, default)`.
    """
    body = {"prompt": "cat", "response_format": bad}
    validate_images_body(body)
    assert "response_format" not in body


@pytest.mark.parametrize("good", ["url", "b64_json"])
def test_a_valid_response_format_survives(good):
    body = {"prompt": "cat", "response_format": good}
    validate_images_body(body)
    assert body["response_format"] == good


def test_an_absent_response_format_is_not_invented():
    """The door owns no default of its own: unspecified stays unspecified.

    Each script answers in the channel's configured shape when the field is
    missing (google's `default_response_format`, everything else its upstream's
    own output), so writing a value here would override a channel decision.
    """
    body = {"prompt": "cat"}
    validate_images_body(body)
    assert "response_format" not in body


def test_an_unhashable_response_format_does_not_raise():
    """A list or an object must not become a 500 on the way to being dropped.

    `x in frozenset` hashes its operand, so `{"type": "json_object"}` -- which a
    chat-shaped caller can put here -- used to raise TypeError.
    """
    body = {"prompt": "cat", "response_format": {"type": "json_object"}}
    validate_images_body(body)
    assert "response_format" not in body


def test_a_mask_without_an_effective_image_is_still_refused():
    """`[]` is an unset image, so a mask has nothing to mask."""
    with pytest.raises(InvalidRequestError) as caught:
        validate_images_body({"image": [], "mask": "data:image/png;base64,AAAA"})
    assert caught.value.param == "mask"


def test_a_real_image_list_survives_untouched():
    refs = ["data:image/png;base64,AAAA", "https://cdn.test/b.png"]
    body = {"prompt": "cat", "image": refs}
    validate_images_body(body)
    assert body["image"] == refs
