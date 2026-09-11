"""Client-request context for the spans an incident is read from.

``adapt`` and ``cascade`` already record *which* channel, script and upstream
URL served a request. They do not record **what was asked for**, and that is
the gap that makes a vendor refusal unactionable: the motivating example is a
content-policy 400 -- the vendor's "the generated image may violate the
restriction on nudity, pornography or erotic content" -- which names neither
the prompt nor the reference image, so the only route back to the offending
request was the caller's own logs.

The attributes below are attached in the span *constructor* rather than after
the upstream call returns. That ordering is the whole point: an attribute
written before a failure survives it, and the failing request is the one worth
reading. ``tests/integration/test_request_context_span.py`` pins exactly that.

The published set, per request. The first group goes on in the span
constructor, from the client's own body:

    prompt                  str, at most PROMPT_LIMIT characters
    prompt_chars            int, the untruncated length, so truncation is visible
    model                   str, the client's model, at most MODEL_LIMIT chars
    image_urls              list[str], the http(s) references only
    image_inline_refs       int, references that carry the image itself
    image_urls_truncated    int, only when the per-field cap dropped some
    mask_urls               list[str], same rules; /v1/images/edits only
    mask_inline_refs        int

and the second group afterwards, from what the script produced:

    result_urls             list[str], the http(s) links in the reply
    result_urls_truncated   int, only when the cap dropped some
    result_inline_refs      int, images that came back as base64

Two further attributes give the span GenAI semantic-convention identity, so it
is read as a model call rather than a generic HTTP request:

    gen_ai.operation.name   "image_generation"
    gen_ai.request.model    the client's `model`, capped at MODEL_LIMIT to match

A span carrying the second of those leaves the exporter with
``gen_ai.response.model`` set to the same value: that is Logfire's defaulting,
not a claim made here. See the note at the assignment below -- the published
set is the list above, and a real trace may show one attribute more.

Two rules decide the shape of everything here.

**Base64 never leaves the process.** A canonical reference is a remote URL, a
data URI or a bare base64 string, and the last two *are* the image. A 20 MB
reference would be a 27 MB attribute exported on every failed request, at a
cost paid by the exporter, the network and whoever reads the trace. Only
http(s) references travel verbatim; inline ones are counted instead, which
still separates "no image was attached" (attribute absent) from "an image was
attached and it was inline" (``image_inline_refs`` set). Every upload through
the multipart door lands in the second case: it normalises files to data URIs.
The same rule covers the reply: a result image is reported as a link when it is
one, and merely counted when the channel answered in base64 -- which is the
normal outcome for ``response_format=b64_json``, and also what an
unconfigured object store degrades to when ``url`` was asked for.

**Nothing here may raise.** It runs before the span that reports the request is
opened, so an unexpected shape raising would turn a request that was going to
succeed into a 500 -- a tracing change that breaks traffic. Every read is
therefore defensive, and a field of the wrong type is treated as absent rather
than stringified. That is also what keeps an unvalidated ``prompt`` out of the
trace: a non-string prompt is accepted whenever an image is present (an
image-only request needs no prompt), and ``str()`` on a nested object would
dump it into the span.

One further case is worth naming, because it is the one this context was
missing longest: a request that never reaches ``execute`` at all. Admission,
body parsing, validation, channel parsing and script loading can each refuse
before any of the spans below exist, and those refusals used to leave behind
nothing but an HTTP status. ``record_ingress_failure`` writes the client
context onto an ``ingress_failed`` span for that case -- carrying the
attributes that were *readable when the request died* and no others, so one
refused before its body was parsed does not appear to have carried a prompt.
The GenAI identity fields are left off: no model call happened.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import logfire

if TYPE_CHECKING:
    from logfire import LogfireSpan

#: Per-value caps. 2000 characters covers a long instruction plus a system
#: preamble on the chat door; the truncation stays visible because
#: ``prompt_chars`` keeps the original length.
PROMPT_LIMIT = 2000
URL_LIMIT = 2000
MODEL_LIMIT = 200

#: Ceiling on the references carried per field. A multi-reference edit is two
#: to four; the multipart door accepts up to 64 files, and 64 URLs of 2000
#: characters is an attribute nobody reads. What the cap drops is reported in
#: ``<field>_urls_truncated`` rather than dropped silently.
URL_COUNT_LIMIT = 8

_URL_PREFIXES = ("http://", "https://")

#: ``gen_ai.operation.name`` for this adapter's one contract: every door folds
#: onto an image request, so every `adapt` call is an image generation.
#:
#: This spelling is **Logfire's**, not the specification's: the GenAI
#: conventions carry no image-specific operation (multimodal generation is
#: ``generate_content`` plus ``gen_ai.output.type=image``), and Logfire's own
#: OpenAI instrumentation uses ``image_generation`` for
#: ``POST /images/generations``. Since Logfire is the reader this exists for,
#: its vocabulary is the one that lights the span up.
#:
#: ``gen_ai.provider.name`` (and its deprecated alias ``gen_ai.system``) is
#: deliberately **not** set: the vocabulary has a value for ``openai`` /
#: ``anthropic`` / ``gcp.vertex_ai`` and none for ``volcengine_ark``, and none
#: at all for a reseller gateway -- whose "provider" is only knowable from the
#: URL the control plane sent. A guessed provider is worse than an absent one,
#: and "no fact" stays a first-class answer here as everywhere else.
OPERATION_IMAGE_GENERATION = "image_generation"


def _split_refs(value: object) -> tuple[list[str], int, int]:
    """One canonical image field -> (urls, inline refs, urls lost to the cap).

    The canonical contract accepts a scalar or a list for both ``image`` and
    ``mask``, so both are read through here.
    """
    items = value if isinstance(value, list) else [value]
    urls: list[str] = []
    inline = 0
    for item in items:
        if not isinstance(item, str):
            continue
        if item.startswith(_URL_PREFIXES):
            urls.append(item[:URL_LIMIT])
        else:
            # A data URI or a bare base64 string: the image itself. A value
            # that is neither is counted here too, deliberately -- it is not a
            # reference anyone could have fetched, so there is nothing about it
            # worth carrying into a span.
            inline += 1
    dropped = max(0, len(urls) - URL_COUNT_LIMIT)
    return urls[:URL_COUNT_LIMIT], inline, dropped


def summarise_request(payload: object, *, genai: bool = True) -> dict[str, Any]:
    """The tracing attributes for one canonical client body.

    ``payload`` is the body the pipeline hands to the executor: already folded
    to the canonical shape by whichever front door the client used, and already
    validated. Wrong-shaped input is still tolerated -- see the module
    docstring -- so an unrecognised payload yields no attributes rather than an
    exception.

    ``genai=False`` withholds the GenAI identity fields. It exists for a span
    describing a request that was refused *before* any upstream call, where
    ``gen_ai.request.model`` would assert a model call that never happened --
    and, through Logfire's exporter defaulting, mint a matching
    ``gen_ai.response.model`` to go with it.
    """
    if not isinstance(payload, dict):
        return {}

    attrs: dict[str, Any] = {}
    if genai:
        attrs["gen_ai.operation.name"] = OPERATION_IMAGE_GENERATION

    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt:
        attrs["prompt"] = prompt[:PROMPT_LIMIT]
        attrs["prompt_chars"] = len(prompt)

    model = payload.get("model")
    if isinstance(model, str) and model:
        attrs["model"] = model[:MODEL_LIMIT]
        # The conventions separate the model that was *requested* from the one
        # that *answered*, which is exactly the distinction this adapter needs
        # to keep: the canonical body's `model` is the client's request, while
        # which model actually runs is the channel's business (the ark script
        # never reads this field at all). Only the requested one is claimed;
        # `gen_ai.response.model` is never set here, because nothing at this
        # layer knows which model answered.
        #
        # Logfire's exporter then derives `gen_ai.response.model` from this
        # attribute whenever the span does not carry one of its own
        # (`_default_gen_ai_response_model`, run on every span end), so a real
        # trace reads as though the requested model answered. That is the
        # platform's defaulting rather than a fact this adapter asserts -- and
        # it is the reason a channel whose script takes the model from its own
        # options (`volcengine_ark`, which drops the body's) will show the
        # client's routing label in that column, not its access point.
        if genai:
            attrs["gen_ai.request.model"] = model[:MODEL_LIMIT]

    for field in ("image", "mask"):
        value = payload.get(field)
        if value is None:
            continue
        urls, inline, dropped = _split_refs(value)
        if urls:
            attrs[f"{field}_urls"] = urls
        if inline:
            attrs[f"{field}_inline_refs"] = inline
        if dropped:
            # Reachable for `image` only: validation requires `mask` to be a
            # single reference (``_check_image_ref`` refuses a list), so its
            # counter is never emitted and is deliberately not published.
            attrs[f"{field}_urls_truncated"] = dropped

    return attrs


def _carrier_of(item: object) -> str | None:
    """The string carrying the image in one canonical ``data[]`` item.

    ``url`` first: it is the field a ``response_format=url`` caller asked for,
    and a script that fills both is answering that caller.
    """
    if not isinstance(item, dict):
        return None
    for key in ("url", "b64_json"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def summarise_result(payload: object) -> dict[str, Any]:
    """The tracing attributes for one canonical client-facing result.

    Only ``data[]`` is read. That is the canonical reply shape (see
    ``adapter/api/frontdoor.py``), and the wrappers step aside for a script that
    answers in the door's native shape instead -- so a chat-shaped or
    responses-shaped reply reports nothing rather than being guessed at. The
    alternative, walking every unknown structure for URL-looking strings, would
    report the caller's *input* echo and the vendor's unrelated links as if
    they were the images produced.
    """
    if not isinstance(payload, dict):
        return {}
    items = payload.get("data")
    if not isinstance(items, list):
        return {}

    urls: list[str] = []
    inline = 0
    for item in items:
        carrier = _carrier_of(item)
        if carrier is None:
            continue
        if carrier.startswith(_URL_PREFIXES):
            urls.append(carrier[:URL_LIMIT])
        else:
            # Base64, or a data URI wearing a `url` key. Counted, not carried
            # -- same rule as the request side, and for the same reason.
            inline += 1

    attrs: dict[str, Any] = {}
    if urls:
        attrs["result_urls"] = urls[:URL_COUNT_LIMIT]
        dropped = max(0, len(urls) - URL_COUNT_LIMIT)
        if dropped:
            attrs["result_urls_truncated"] = dropped
    if inline:
        attrs["result_inline_refs"] = inline
    return attrs


def record_result(span: LogfireSpan, result: object) -> None:
    """Writes the reply's image links onto the span that is still open.

    Separate from the constructor attributes because there is nothing to report
    until the script has shaped the reply. A request that failed never gets
    here, so an error span simply carries no result attributes -- the right
    reading, since no image was produced.
    """
    for key, value in summarise_result(result).items():
        span.set_attribute(key, value)


def record_ingress_failure(
    endpoint: str, stage: str, payload: object, exc: Exception
) -> None:
    """Reports a request a front door refused before ``execute`` ever ran.

    The counterpart of ``record_result``: that one answers "where is the
    picture" for a call that succeeded, this one answers "what was asked for"
    for a call that never reached the upstream. Between them, a request leaves
    a span carrying its prompt however far it got.

    ``stage`` names how far it *did* get -- ``admission`` / ``body`` /
    ``validation`` / ``channel`` / ``script`` -- and is the honest half of this
    function. The context is summarised from the payload as it stood when the
    request died, so one refused at admission carries no ``prompt`` at all
    rather than an empty one. The attribute set is the root span's minus
    ``gen_ai.*``, and is *sparse by outcome* rather than uniformly present.

    A refusal at the front door is not a model call, so the GenAI identity
    fields stay off; claiming otherwise would put a fiction in the one place an
    incident gets read from.
    """
    attrs: dict[str, Any] = {"endpoint": endpoint, "stage": stage}
    status = getattr(exc, "status", None)
    if status is not None:
        attrs["status"] = status
    attrs["error_code"] = getattr(exc, "code", None) or "error"
    attrs.update(summarise_request(payload, genai=False))

    with logfire.span("ingress_failed", **attrs):
        pass


# --- span timing -----------------------------------------------------------


@contextlib.contextmanager
def span_elapsed_ms(span: LogfireSpan) -> Iterator[None]:
    """Records ``elapsed_ms`` on the span when the block ends, either way.

    A context manager rather than a trailing ``set_attribute`` so that a failed
    block is timed too: the slow path is the one an incident is read for, and
    it is exactly the path that never reaches a statement after the ``await``.

    Used alongside the span, not instead of it::

        with logfire.span("upstream_call", url=url) as span, span_elapsed_ms(span):
            ...
    """
    started = time.monotonic()
    try:
        yield
    finally:
        span.set_attribute("elapsed_ms", _ms_since(started))


@contextlib.contextmanager
def phase_summary(span: LogfireSpan, ctx: Any) -> Iterator[None]:
    """Adds the per-phase wall-clock breakdown to a request span as it closes.

    Answers what the ``script_phase`` spans cannot on their own -- "of this
    request's total, how much was the script at all" -- and does so for the
    failing request too, which is why it is a context manager and not a write
    before the ``return``: the breakdown matters most when the request did
    *not* finish.

    ``phase_ms`` is cumulative per phase name and ``phase_calls`` counts the
    calls, because the polled phases repeat. One 30 s ``poll_response`` and
    thirty 1 s ones accumulate alike, and only the count tells them apart.
    Both are dicts, which the exporter flattens to JSON text -- see the note in
    docs/03 §5.2 -- and an absent pair means no phase ran at all.
    """
    try:
        yield
    finally:
        times = getattr(ctx, "phase_ms", None)
        if times:
            span.set_attribute("phase_ms", dict(times))
            calls = getattr(ctx, "phase_calls", None)
            if calls:
                span.set_attribute("phase_calls", dict(calls))


def _ms_since(started: float) -> float:
    """Milliseconds since a ``time.monotonic()`` reading, to one decimal.

    The decimal is kept because the interesting comparison is often between two
    near-instant lookups; anything beyond it is noise contributed by the
    exporter rather than a measurement of the work.
    """
    return round((time.monotonic() - started) * 1000.0, 1)
