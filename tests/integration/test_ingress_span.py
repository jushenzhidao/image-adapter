"""A refusal that never reaches ``execute`` still names what was asked for.

The client context exists because a content-policy 400 is unactionable: the
vendor's message names neither prompt nor reference image. What
``test_request_context_span.py`` pins is that context surviving an *upstream*
refusal. This file covers the other half -- the five front-door steps
(admission, body parsing, validation, channel parsing, script loading) that
refuse a request before any span exists at all.

Two things are pinned, and the second matters as much as the first:

* the context that *was* readable at the point of failure reaches the
  ``ingress_failed`` span;
* the context that *was not* readable does not appear at all -- a request
  refused at admission carries no ``prompt``, rather than an empty one. An
  attribute set that looks uniform would make a never-read prompt
  indistinguishable from a genuinely absent one.

Driven through ``adapt`` rather than the app, for the reason
``test_request_context_span.py`` gives: the app's lifespan reconfigures Logfire
and would detach the exporter these assertions read. ``adapt`` only touches a
small, duck-typed surface of the request, so a stand-in carries it -- and
``TestClient`` is avoided precisely because it starts that lifespan.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import logfire
import pytest
from logfire.testing import (
    METRICS_PREFERRED_TEMPORALITY,
    IncrementalIdGenerator,
    InMemoryMetricReader,
    SimpleLogRecordProcessor,
    SimpleSpanProcessor,
    TestExporter,
    TestLogExporter,
    TimeGenerator,
)
from starlette.datastructures import Headers

from adapter.api.images import validate_images_body
from adapter.api.pipeline import adapt
from adapter.errors import AdapterError
from adapter.script_cache import CompiledScript
from adapter.settings import Settings

PROMPT = "a cat wearing a hat"
IMAGE_URL = "https://refs.example.test/source.png"

ADAPTER_KEY = "admission-key"
INLINE_SOURCE = "def transform(ctx, payload, phase):\n    return dict(payload)\n"


class _Request:
    """The surface ``adapt`` reads off a request, and nothing more."""

    def __init__(
        self,
        settings: Settings,
        *,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        script_cache: object = None,
    ) -> None:
        self._body = body
        self.headers = Headers(headers or {})
        self.state = SimpleNamespace()
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                script_cache=script_cache,
                script_store=None,
                http=None,
                asset_cache=None,
                storage=None,
            )
        )

    async def body(self) -> bytes:
        return self._body


class _Cache:
    """Stands in for the compiled-script cache, so no sandbox run is needed."""

    def __init__(self, script: CompiledScript) -> None:
        self._script = script

    def load(self, source: object) -> CompiledScript:
        return self._script


class _Unreachable(BaseHTTPRequestHandler):
    """A socket that answers, so the failure under test is the *upstream* one."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        raw = b'{"error": {"message": "nope"}}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def settings() -> Settings:
    """Every field deciding what these tests exercise is stated explicitly:
    Settings reads .env, so a local override would otherwise change the
    behaviour under test rather than fail a test visibly."""
    return Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        fal_key="",
    )


@pytest.fixture
def spans() -> TestExporter:
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        advanced=logfire.AdvancedOptions(
            id_generator=IncrementalIdGenerator(),
            ns_timestamp_generator=TimeGenerator(),
            log_record_processors=[
                SimpleLogRecordProcessor(TestLogExporter(TimeGenerator()))
            ],
        ),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        metrics=logfire.MetricsOptions(
            additional_readers=[
                InMemoryMetricReader(
                    preferred_temporality=METRICS_PREFERRED_TEMPORALITY
                )
            ]
        ),
    )
    return exporter


async def _run(
    settings: Settings,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    prepare=None,
    script_cache: object = None,
) -> str:
    """One request through ``adapt``, as an outcome string."""
    request = _Request(
        settings,
        body=json.dumps(payload).encode(),
        headers=headers,
        script_cache=script_cache,
    )
    try:
        await adapt(request, "images", prepare)
        return "ok"
    except AdapterError as exc:
        return f"err:{exc.code}"


def _attributes(exporter: TestExporter, name: str = "ingress_failed") -> dict:
    logfire.force_flush()
    found = [span for span in exporter.exported_spans_as_dict() if span["name"] == name]
    assert len(found) == 1, f"expected one {name} span, got {len(found)}"
    return found[0]["attributes"]


def _urls(attributes: dict, name: str) -> list[str]:
    """A list-valued attribute, back out of Logfire's own serialisation.

    Logfire renders a non-primitive argument as JSON text and records its
    intended shape in a companion ``logfire.json_schema`` attribute. On the raw
    span it is therefore a string, so the assertion parses it.
    """
    return json.loads(attributes[name])


# --- what reaches the span ------------------------------------------------


async def test_an_admission_refusal_reports_itself_and_claims_no_prompt(
    settings, spans
):
    """The body was never parsed, so the span must not pretend otherwise.

    An empty or absent ``prompt`` here is the honest reading: nothing was read
    yet. This is also the exact opposite ordering from the upstream-refusal
    case, which is why both are pinned.
    """
    outcome = await _run(
        settings,
        {"prompt": PROMPT, "image": IMAGE_URL},
        headers={"x-upstream-url": "https://api.example.test/v1/images"},
    )

    assert outcome == "err:invalid_adapter_key"

    attributes = _attributes(spans)
    assert attributes["stage"] == "admission"
    assert attributes["status"] == 401
    assert attributes["error_code"] == "invalid_adapter_key"
    assert "prompt" not in attributes
    assert "image_urls" not in attributes


async def test_a_validation_refusal_keeps_the_context_that_was_read(
    settings, spans
):
    """The case this whole change is for.

    ``mask`` without ``image`` is refused by the front door, so no ``adapt``
    span is ever opened -- yet both the prompt and the mask reference are in
    the body the request died with.
    """
    outcome = await _run(
        settings,
        {"prompt": PROMPT, "mask": IMAGE_URL},
        headers={"x-adapter-key": ADAPTER_KEY},
        prepare=validate_images_body,
    )

    assert outcome == "err:invalid_request"

    attributes = _attributes(spans)
    assert attributes["stage"] == "validation"
    assert attributes["status"] == 400
    assert attributes["prompt"] == PROMPT
    assert attributes["prompt_chars"] == len(PROMPT)
    assert _urls(attributes, "mask_urls") == [IMAGE_URL]


async def test_a_channel_refusal_keeps_the_context_that_was_read(settings, spans):
    """A missing X-Upstream-Url is a control-plane bug, not a client one --
    and without the prompt there is nothing to reproduce it with."""
    outcome = await _run(
        settings,
        {"prompt": PROMPT},
        headers={"x-adapter-key": ADAPTER_KEY},
    )

    assert outcome == "err:channel_config_error"

    attributes = _attributes(spans)
    assert attributes["stage"] == "channel"
    assert attributes["prompt"] == PROMPT


async def test_a_script_refusal_keeps_the_context_that_was_read(settings, spans):
    """Remote refs are off by default, so this needs no network at all."""
    outcome = await _run(
        settings,
        {"prompt": PROMPT},
        headers={
            "x-adapter-key": ADAPTER_KEY,
            "x-upstream-url": "https://api.example.test/v1/images",
            "x-script-ref": "https://scripts.example.test/adapter.py",
        },
    )

    assert outcome == "err:script_forbidden"

    attributes = _attributes(spans)
    assert attributes["stage"] == "script"
    assert attributes["status"] == 403
    assert attributes["prompt"] == PROMPT


# --- what does not reach it ------------------------------------------------


async def test_a_request_that_clears_the_front_door_opens_no_ingress_span(
    settings, spans
):
    """The backstop against recording successes as well.

    Without this, an implementation that opened ``ingress_failed`` on every
    request would satisfy every assertion above. The upstream here answers 500,
    so the request fails *after* the front door -- which is the ``adapt``
    span's business, not this one's.
    """
    server = HTTPServer(("127.0.0.1", 0), _Unreachable)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    script = CompiledScript(
        sha256="0" * 64,
        origin="inline",
        transform=lambda ctx, payload, phase: dict(payload),
    )
    try:
        outcome = await _run(
            settings,
            {"prompt": PROMPT},
            headers={
                "x-adapter-key": ADAPTER_KEY,
                "x-upstream-url": f"http://127.0.0.1:{server.server_port}/v1/images",
                "x-script": INLINE_SOURCE,
            },
            script_cache=_Cache(script),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert outcome == "err:upstream_http_error"

    logfire.force_flush()
    names = [span["name"] for span in spans.exported_spans_as_dict()]
    assert "adapt" in names
    assert "ingress_failed" not in names


async def test_the_ingress_span_does_not_claim_a_model_call(settings, spans):
    """No model ran, so no GenAI identity is asserted.

    ``gen_ai.request.model`` would also mint a matching
    ``gen_ai.response.model`` through Logfire's exporter defaulting, reading as
    though a model had answered a request that never left the process.
    """
    await _run(
        settings,
        {"prompt": PROMPT, "model": "seedream-3.0", "mask": IMAGE_URL},
        headers={"x-adapter-key": ADAPTER_KEY},
        prepare=validate_images_body,
    )

    attributes = _attributes(spans)
    assert attributes["model"] == "seedream-3.0"
    assert "gen_ai.operation.name" not in attributes
    assert "gen_ai.request.model" not in attributes
