"""The prompt and the reference images are what make a failed span readable.

Base64 is the interesting case in both directions. A data URI or a bare base64
string *is* the image, so carrying it verbatim would put a multi-megabyte
attribute on every request; dropping it silently would make "no image was
attached" indistinguishable from "an inline image was attached" -- and every
upload through the multipart door is the second case. So it is counted.

Two groups below pin properties that are not about content at all. The names
are a contract: ``adapt`` and ``cascade`` splat whatever this returns into a
span constructor, so a key that collides with one of their own arguments is a
TypeError on every request rather than a bad attribute. And the function runs
before the span that reports the request is opened, so it must not raise on
anything at all.
"""

from __future__ import annotations

import pytest

from adapter.trace_attrs import (
    MODEL_LIMIT,
    PROMPT_LIMIT,
    URL_COUNT_LIMIT,
    URL_LIMIT,
    summarise_request,
    summarise_result,
)

PNG_DATA_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
BARE_B64 = "iVBORw0KGgoAAAANSUhEUg=="
REMOTE = "https://example.test/a.png"

#: Every name this module may emit. Kept separate from the module docstring so
#: a new attribute cannot be introduced without deciding to publish it.
PUBLISHED = frozenset(
    {
        "prompt",
        "prompt_chars",
        "model",
        "image_urls",
        "image_inline_refs",
        "image_urls_truncated",
        "mask_urls",
        "mask_inline_refs",
        "result_urls",
        "result_urls_truncated",
        "result_inline_refs",
        "gen_ai.operation.name",
        "gen_ai.request.model",
    }
)


def test_a_remote_reference_travels_verbatim():
    attrs = summarise_request({"prompt": "a cat", "image": REMOTE})
    assert attrs["image_urls"] == [REMOTE]
    assert "image_inline_refs" not in attrs


@pytest.mark.parametrize(
    "inline", [PNG_DATA_URI, BARE_B64], ids=["data-uri", "bare-base64"]
)
def test_an_inline_reference_is_counted_and_never_carried(inline):
    attrs = summarise_request({"prompt": "a cat", "image": inline})
    assert "image_urls" not in attrs
    assert attrs["image_inline_refs"] == 1
    # Nothing in the result carries the reference itself.
    assert inline not in "".join(str(value) for value in attrs.values())


def test_a_mixed_list_reports_the_urls_and_counts_the_rest():
    attrs = summarise_request(
        {
            "prompt": "a cat",
            "image": [REMOTE, PNG_DATA_URI, "https://example.test/b.png", BARE_B64],
        }
    )
    assert attrs["image_urls"] == [REMOTE, "https://example.test/b.png"]
    assert attrs["image_inline_refs"] == 2


def test_the_mask_is_classified_by_the_same_rules():
    attrs = summarise_request(
        {"prompt": "a cat", "image": REMOTE, "mask": BARE_B64}
    )
    assert attrs["image_urls"] == [REMOTE]
    assert attrs["mask_inline_refs"] == 1
    assert "mask_urls" not in attrs


def test_an_unset_image_is_not_an_attribute_rather_than_an_empty_one():
    """Absent means "no image was attached"; ``[]`` would say nothing useful."""
    attrs = summarise_request({"prompt": "a cat"})
    assert "image_urls" not in attrs
    assert "image_inline_refs" not in attrs


def test_a_long_prompt_is_truncated_and_its_length_survives():
    """Truncation has to stay visible, or it reads as the whole prompt."""
    attrs = summarise_request({"prompt": "x" * (PROMPT_LIMIT + 500)})
    assert len(attrs["prompt"]) == PROMPT_LIMIT
    assert attrs["prompt_chars"] == PROMPT_LIMIT + 500


def test_a_long_url_is_truncated():
    attrs = summarise_request({"prompt": "x", "image": "https://e.test/" + "a" * 5000})
    assert len(attrs["image_urls"][0]) == URL_LIMIT


def test_the_url_cap_states_what_it_dropped():
    refs = [f"https://example.test/{i}.png" for i in range(URL_COUNT_LIMIT + 3)]
    attrs = summarise_request({"prompt": "x", "image": refs})
    assert len(attrs["image_urls"]) == URL_COUNT_LIMIT
    assert attrs["image_urls_truncated"] == 3


def test_the_url_cap_is_absent_when_it_drops_nothing():
    refs = [f"https://example.test/{i}.png" for i in range(URL_COUNT_LIMIT)]
    attrs = summarise_request({"prompt": "x", "image": refs})
    assert "image_urls_truncated" not in attrs


def test_the_model_is_carried_and_capped():
    attrs = summarise_request({"prompt": "x", "model": "z" * (MODEL_LIMIT + 1)})
    assert attrs["model"] == "z" * MODEL_LIMIT


# --- GenAI semantic-convention identity -----------------------------------


def test_the_span_declares_itself_an_image_generation():
    """Logfire reads a span as a model call from these, not from how the span
    was produced: its own instrumentation sets them the same way."""
    attrs = summarise_request({"prompt": "a cat"})
    assert attrs["gen_ai.operation.name"] == "image_generation"


def test_the_requested_model_is_reported_in_the_convention_spelling():
    attrs = summarise_request({"prompt": "a cat", "model": "seedream-3.0"})
    assert attrs["gen_ai.request.model"] == "seedream-3.0"


def test_no_model_means_no_gen_ai_model_attribute():
    """Absent, not empty: nothing here knows which model ran."""
    attrs = summarise_request({"prompt": "a cat"})
    assert "gen_ai.request.model" not in attrs


@pytest.mark.parametrize(
    "name",
    ["gen_ai.provider.name", "gen_ai.system", "gen_ai.response.model"],
)
def test_no_model_identity_is_invented(name):
    """The provider vocabulary has no value for `volcengine_ark` and none at
    all for a reseller gateway, and the answering model is the channel's
    business. A guessed value would read as fact in the trace; absent does not.
    """
    attrs = summarise_request({"prompt": "a cat", "model": "seedream-3.0"})
    assert name not in attrs


def test_an_empty_prompt_is_not_an_attribute():
    assert "prompt" not in summarise_request({"prompt": ""})


@pytest.mark.parametrize(
    ("summarise", "payload"),
    [
        (
            summarise_request,
            {
                "prompt": "a cat",
                "model": "m",
                "image": [REMOTE, PNG_DATA_URI],
                "mask": REMOTE,
            },
        ),
        (summarise_result, {"data": [{"url": REMOTE}, {"b64_json": BARE_B64}]}),
    ],
    ids=["request", "result"],
)
def test_every_attribute_is_one_that_was_published(summarise, payload):
    assert set(summarise(payload)) <= PUBLISHED


# --- the reply side -------------------------------------------------------


def test_a_result_link_is_reported():
    """The other half of "something went wrong": the picture the caller asked
    for is a URL, so the trace can carry it without carrying the bytes."""
    attrs = summarise_result({"data": [{"url": REMOTE}]})
    assert attrs["result_urls"] == [REMOTE]
    assert "result_inline_refs" not in attrs


def test_a_base64_result_is_counted_and_never_carried():
    """What `response_format=b64_json` produces, and what an unconfigured
    object store degrades a `url` request to."""
    attrs = summarise_result({"data": [{"b64_json": BARE_B64}]})
    assert "result_urls" not in attrs
    assert attrs["result_inline_refs"] == 1
    assert BARE_B64 not in "".join(str(value) for value in attrs.values())


def test_a_data_uri_under_the_url_key_is_counted_not_reported():
    """The adapter never dresses a data URI up as a link, so one sitting under
    `url` is base64 by another name."""
    attrs = summarise_result({"data": [{"url": PNG_DATA_URI}]})
    assert "result_urls" not in attrs
    assert attrs["result_inline_refs"] == 1


def test_a_mixed_reply_reports_the_links_and_counts_the_rest():
    attrs = summarise_result(
        {"data": [{"url": REMOTE}, {"b64_json": BARE_B64}, {"url": "https://b.test/y"}]}
    )
    assert attrs["result_urls"] == [REMOTE, "https://b.test/y"]
    assert attrs["result_inline_refs"] == 1


def test_a_result_carried_by_neither_field_is_not_an_image():
    attrs = summarise_result({"data": [{"size": "2K"}, {"revised_prompt": "a cat"}]})
    assert attrs == {}


def test_a_non_canonical_reply_reports_nothing():
    """A script may answer in the door's native shape, and the wrappers step
    aside for it; guessing at unknown structures would report the caller's own
    echoed input as if the vendor had produced it."""
    attrs = summarise_result(
        {"choices": [{"message": {"content": [{"image_url": {"url": REMOTE}}]}}]}
    )
    assert attrs == {}


def test_the_result_cap_states_what_it_dropped():
    items = [{"url": f"https://example.test/{i}.png"} for i in range(URL_COUNT_LIMIT + 2)]
    attrs = summarise_result({"data": items})
    assert len(attrs["result_urls"]) == URL_COUNT_LIMIT
    assert attrs["result_urls_truncated"] == 2


def test_a_long_result_url_is_truncated():
    attrs = summarise_result({"data": [{"url": "https://e.test/" + "a" * 5000}]})
    assert len(attrs["result_urls"][0]) == URL_LIMIT


@pytest.mark.parametrize(
    "own",
    [
        {
            "endpoint": "images",
            "script_sha256": "abc123",
            "script_origin": "ref",
            "upstream_url": "https://up.test/v1/images/generations",
            "is_async": False,
            "stage": None,
        },
        {
            "endpoint": "images",
            "script_sha256": "abc123",
            "stages": ["generate"],
            "budget_s": 300,
        },
    ],
    ids=["adapt", "cascade"],
)
def test_the_attributes_fit_both_span_constructors(own):
    """A name collision here is a TypeError on every request, so it is pinned.

    Both call sites splat this result next to arguments of their own; this
    builds each span the way the code does, with every attribute present. It
    exports nothing -- nothing is asserted about the span, only that
    constructing it did not raise -- but Logfire has to be configured or the
    SDK warns on every span.
    """
    import logfire

    logfire.configure(send_to_logfire=False, console=False)

    attrs = summarise_request(
        {"prompt": "a cat", "model": "m", "image": [REMOTE, PNG_DATA_URI], "mask": REMOTE}
    )
    with logfire.span("span-under-test", **own, **attrs):
        pass


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "prompt=cat",
        42,
        # A non-string prompt is accepted whenever an image is present, so this
        # reaches the summariser; str() on it would dump the object into a span.
        {"prompt": {"nested": "object"}},
        {"prompt": "x", "image": {"url": REMOTE}},
        {"prompt": "x", "image": [None, 17, {"a": 1}]},
        {"prompt": "x", "model": ["m"]},
        {"prompt": "x", "mask": 3},
    ],
    ids=[
        "null",
        "list",
        "string",
        "number",
        "object-prompt",
        "object-image",
        "list-of-junk",
        "list-model",
        "number-mask",
    ],
)
def test_no_shape_of_input_raises(payload):
    """Runs before the reporting span exists: an exception would 500 a good
    request, which is a tracing change that breaks traffic."""
    assert isinstance(summarise_request(payload), dict)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "data",
        {"data": "not a list"},
        {"data": [None, 3, "url", {"url": 5}]},
        {"data": [{"url": None, "b64_json": None}]},
    ],
    ids=["null", "list", "string", "data-string", "junk-items", "empty-fields"],
)
def test_no_shape_of_reply_raises(payload):
    """Runs while the span is open, after the response phase: raising here
    would turn a produced image into a 500."""
    assert isinstance(summarise_result(payload), dict)
