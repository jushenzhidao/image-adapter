"""The redaction policy: api keys only, everything else verbatim.

logfire's default patterns are broad -- ``secret``, ``password``, ``session``,
``cookie``, ``credential``, ``jwt`` and more -- and a match replaces the **whole**
value, not the matched word. That is what turned every minio presigned result link
into ``[Scrubbed due to 'Credential']``, bucket and key included. The callback in
``adapter/logfire_setup`` cancels that for every match that is not an api-key name,
so the pass now protects exactly one class of thing.

Both directions are pinned here, and both fail silently otherwise: a policy that
redacts too much eats the prompt a refused request is read for (the whole point of
the attribute set), and one that redacts too little exports a working key.
"""

from __future__ import annotations

import json
import os
import re

import logfire
import pytest
from fastapi import FastAPI
from logfire.testing import SimpleSpanProcessor, TestExporter

from adapter.logfire_setup import (
    _SCRUB_PATTERNS,
    _scrubbing_callback,
    flush_spans,
    init_logfire,
)
from adapter.settings import Settings

# A SigV4 presigned GET, in the shape minio-py produces (see
# adapter/storage/minio_store.py). Its query string carries `X-Amz-Credential`,
# which is the substring logfire's default patterns match.
#
# The credential id is spelled out rather than copied from AWS documentation:
# `AKIA` + 16 characters is a real key's shape, and a scanner cannot tell a
# placeholder from a live one.
SIGNED = (
    "https://minio.internal:9000/adapter-temp/temp/req-1/out.png"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=minio-example%2F20260912%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260912T031500Z&X-Amz-Expires=604800"
    "&X-Amz-SignedHeaders=host&X-Amz-Signature=deadbeefcafe"
)

#: A prompt that mentions every word logfire would normally redact. It has to come
#: back byte-for-byte: this attribute is the reason the request context exists.
PROSE = (
    "a poster about api key hygiene, the authorization process, "
    "password policy, session handling and cookie consent"
)

VENDOR_KEY = "sk-live-should-never-leave"
ADAPTER_KEY = "ak_live_9f3a2b7c1d"


def _settings(**overrides) -> Settings:
    """A ``Settings`` isolated from ``.env`` for the fields that matter here."""
    base = {
        "environment": "dev",
        # Without this, a token in .env would have these tests export probe spans
        # to the real service.
        "logfire_token": "",
        "redis_url": "",
        "minio_endpoint": "",
        "fal_key": "",
    }
    return Settings(**{**base, **overrides})


@pytest.fixture
def spans() -> TestExporter:
    """Points Logfire at an in-memory exporter, with the real scrubbing config.

    ``scrubbing=`` is spelled out from the module's own objects rather than left
    to ``init_logfire``: this file asserts what those objects do, and
    ``test_init_logfire_wires_the_patterns_and_the_callback`` separately asserts
    that ``init_logfire`` is what passes them.
    """
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        scrubbing=logfire.ScrubbingOptions(
            extra_patterns=list(_SCRUB_PATTERNS), callback=_scrubbing_callback
        ),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    return exporter


def _emit(attributes: dict) -> None:
    with logfire.span("probe") as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)


def _attributes(exporter: TestExporter) -> dict:
    """The attributes of the single probe span, as Logfire exports them."""
    logfire.force_flush()
    found = [
        span for span in exporter.exported_spans_as_dict() if span["name"] == "probe"
    ]
    assert len(found) == 1, f"expected one probe span, got {len(found)}"
    return found[0]["attributes"]


def _urls(exporter: TestExporter, name: str) -> list[str]:
    """A list attribute back out of Logfire's own serialisation.

    A non-primitive attribute is exported as JSON text plus a companion
    ``logfire.json_schema``, which is what the UI reads to show an array.
    """
    return json.loads(_attributes(exporter)[name])


# --- what is kept ---------------------------------------------------------


def test_a_presigned_link_arrives_verbatim(spans):
    """The value that started this: `credential` no longer rewrites the URL."""
    _emit({"result_urls": [SIGNED]})

    assert _urls(spans, "result_urls") == [SIGNED]


def test_prose_is_left_alone(spans):
    """The direction that is easy to lose: a prompt must come back byte-for-byte.

    Every word in ``PROSE`` matches one of logfire's default patterns. Without the
    callback each one would have replaced the whole attribute -- and the prompt is
    what makes a vendor refusal actionable.
    """
    _emit({"prompt": PROSE})

    attributes = _attributes(spans)
    assert attributes["prompt"] == PROSE
    assert "logfire.scrubbed" not in attributes


def test_credential_words_that_are_not_key_names_survive(spans):
    """``password``, ``session``, ``secret``: no longer redacted, by request.

    One match replaces the whole value, so this also pins that a single
    unwelcome word (``password`` here, which logfire matches first) does not take
    the rest of the value with it.
    """
    notes = "password=hunter2 session=abc secret: xyz credential=AKIAEXAMPLE"
    _emit({"notes": notes})

    assert _attributes(spans)["notes"] == notes


# --- what is redacted -----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["authorization", "api_key", "x-adapter-key", "x-auth-emit", "x-script"],
    ids=["authorization", "api-key", "adapter-key", "auth-emit", "script"],
)
def test_an_attribute_named_after_a_key_is_redacted(spans, name):
    """Names are structured, so a name match needs no assignment to be believed.

    This is the shape ``capture_headers=True`` would produce
    (``http.request.header.x-adapter-key``), which is refused -- but these names
    also reach a span when someone logs a header line into a value.
    """
    _emit({name: VENDOR_KEY})

    assert _attributes(spans)[name].startswith("[Scrubbed due to ")


def test_a_key_assignment_inside_a_value_is_redacted(spans):
    """The realistic path for a key into a span: a query string.

    ``X-Auth-Emit: query:...`` puts the upstream credential in the URL, and
    ``upstream_call.url`` is a published attribute.
    """
    _emit({"url": f"https://vendor.test/v1/gen?api_key={VENDOR_KEY}"})

    assert VENDOR_KEY not in _attributes(spans)["url"]


def test_a_whole_value_header_line_is_redacted(spans):
    """The shape the patterns used to miss, and the reason they lost their tail.

    A header line that *is* the whole value used to match end to end; logfire
    reads a whole-string match as safe, so the callback was never consulted and
    the credential stayed. Pinned as the inverse of the defect it replaced.
    """
    _emit({"headers_echo": f"X-Adapter-Key: {ADAPTER_KEY}"})

    assert ADAPTER_KEY not in _attributes(spans)["headers_echo"]


def test_the_redactions_are_recorded(spans):
    """A redaction leaves its note, so a reader knows a value was replaced."""
    _emit({"api_key": VENDOR_KEY})

    notes = json.loads(_attributes(spans)["logfire.scrubbed"])

    assert [note["path"] for note in notes] == [["attributes", "api_key"]]


# --- the callback's own predicate -----------------------------------------


@pytest.mark.parametrize(
    "text",
    ["password", "secret", "session", "cookie", "credential", "jwt", "ssn"],
)
def test_the_callback_declines_every_word_that_is_not_a_key_name(text):
    assert _scrubbing_callback(_match(("attributes", "notes"), text, text)) == text


def test_the_callback_declines_a_key_name_inside_prose():
    """`api_key` in a sentence is a word, not a credential -- the tail decides."""
    value = "draw a poster advertising an api_key service"

    assert (
        _scrubbing_callback(_match(("attributes", "prompt"), value, "api_key")) == value
    )


def test_the_callback_redacts_when_the_name_is_the_attribute():
    """A name match is searched in the *name*, which is why `text=` is passed.

    The scrubber has two branches -- the mapping one searches the key, the string
    one searches the value -- and a probe that always searched the value would be
    testing a shape that never occurs.
    """
    assert (
        _scrubbing_callback(
            _match(("attributes", "api_key"), VENDOR_KEY, "api_key", text="api_key")
        )
        is None
    )


def test_the_callback_redacts_an_assignment():
    value = f"https://vendor.test/v1/gen?api_key={VENDOR_KEY}"

    assert _scrubbing_callback(_match(("attributes", "url"), value, "api_key")) is None


def test_the_rest_of_the_name_is_read_before_the_separator():
    """logfire matches `auth` inside `authorization`; the tail is not just `:`.

    A predicate that looked only at the character after the match would decline
    here and let the credential through.
    """
    value = f"authorization: Bearer {VENDOR_KEY}"

    assert (
        _scrubbing_callback(_match(("attributes", "headers_echo"), value, "auth"))
        is None
    )


def test_a_key_named_attribute_is_redacted_whatever_the_value_type():
    """A name match redacts the value wholesale, so the type cannot matter."""
    assert (
        _scrubbing_callback(
            _match(("attributes", "api_key"), ["a", "b"], "api_key", text="api_key")
        )
        is None
    )


# --- wiring ---------------------------------------------------------------


def test_init_logfire_wires_the_patterns_and_the_callback():
    """The objects above are only in force if ``init_logfire`` passes them.

    Without this, the callback could be perfect and never run, and the adapter
    would be back to redacting prompts -- silently, since nothing else in the
    suite reads the configured instance.
    """
    init_logfire(FastAPI(), _settings())

    scrubbing = logfire.DEFAULT_LOGFIRE_INSTANCE.config.scrubbing
    assert scrubbing.callback is _scrubbing_callback
    assert set(_SCRUB_PATTERNS) <= set(scrubbing.extra_patterns)


def test_the_batch_interval_is_set_in_code_not_the_environment():
    """It has no other home: logfire reads the variable and takes no argument.

    Deleting the line would silently restore logfire's 500 ms default -- ten times
    the export requests -- so the value is pinned where it is set. Overriding the
    variable deliberately means updating this line too.
    """
    assert os.environ["OTEL_BSP_SCHEDULE_DELAY"] == "5000"


def test_header_capture_is_refused():
    """The one setting that would export credentials, and no list can cover it.

    ``X-Auth-Emit`` names the credential header per channel (``x-goog-api-key``
    for the Google channel), so a fixed redaction list cannot be complete, and a
    worker that starts is a worker nobody re-reads the startup log of.
    """
    with pytest.raises(ValueError, match="LOGFIRE_CAPTURE_HEADERS"):
        init_logfire(FastAPI(), _settings(logfire_capture_headers=True))


def test_the_shutdown_flush_reports_whether_it_finished(spans):
    """``flush_spans`` is what keeps the batching window from being a loss window.

    The batch exporter is a daemon thread with no ``atexit`` hook, so this is the
    only thing that drains it. A deadline that ran out is reported (``False``),
    not raised: shutdown is already under way.
    """
    assert flush_spans() is True
    assert flush_spans(timeout_millis=0) is False


def _match(path: tuple, value: object, matched: str, *, text: str | None = None):
    """A ``ScrubMatch`` as the scrubber would build it for ``path``.

    ``text`` overrides what the pattern is searched in, for values that are not
    strings -- logfire only ever searches the ``str`` branch, so the probe still
    needs a match object to hand to the callback.
    """
    haystack = str(value) if text is None else text
    pattern_match = re.search(re.escape(matched), haystack, re.IGNORECASE)
    assert pattern_match is not None, "the probe needs the pattern to match"
    return logfire.ScrubMatch(path=path, value=value, pattern_match=pattern_match)
