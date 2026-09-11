"""``validate_images_body`` normalises what it can instead of refusing it.

Two shapes mean "the client had nothing to say": an unset image arrives as
``null`` or as ``[]`` depending on the SDK, and an unusable ``n`` arrives as
whatever the caller put there. Both are normalised -- the key is dropped, or
``n`` becomes one -- because refusing an otherwise complete text-to-image
request over either costs the caller its answer and teaches it nothing.

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


def test_a_bad_response_format_is_still_refused():
    with pytest.raises(InvalidRequestError) as caught:
        validate_images_body({"prompt": "cat", "response_format": "hologram"})
    assert caught.value.param == "response_format"


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
