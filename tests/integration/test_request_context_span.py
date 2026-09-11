"""What was asked for -- and what came back -- reaches the trace.

The client context is only worth attaching in the case where it is needed, and
that case is a refusal: the content-policy 400 that motivated this work names
neither the prompt nor the reference image it rejected. The reply side answers
the other half -- "the call succeeded, so where is the picture" -- which is
why a finished image's link is reported too.

The distinction matters. Asserting a successful request alone would pass even
if the attributes were attached *after* the upstream call returned, which is
exactly the arrangement that loses them -- and would have left this feature
looking green while being useless. So the refusal path is pinned first.

Driven at ``execute`` / ``execute_staged`` rather than through the app, for the
same reason ``test_upstream_span.py`` drives ``_do_upstream``: the app's
lifespan reconfigures Logfire and would detach the exporter these assertions
read, and the functions under test own the spans anyway. The upstream is a real
socket, so the transport is exercised rather than replaced by a stand-in.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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

from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.executor import execute
from adapter.script_cache import CompiledScript
from adapter.settings import Settings
from adapter.stages import execute_staged

#: What the vendor says when it refuses. Verbatim from the incident that
#: prompted this feature: it names no prompt and no image.
REFUSAL = (
    "非常抱歉，生成的图片可能违反了关于裸露、色情或情色内容的防护限制。"
    "如果你认为此判断有误，请重试或修改提示语。"
)

PROMPT = "a cat wearing a hat"
IMAGE_URL = "https://refs.example.test/source.png"
INLINE_IMAGE = "data:image/png;base64," + "A" * 4096

OUTPUT_URL = "https://cdn.example.test/out.png"
OUTPUT_B64 = "iVBORw0KGgoAAAANSUhEUg=="


class _Vendor(BaseHTTPRequestHandler):
    """Refuses, or answers with one image, per the mode the test asked for."""

    mode = "refuse"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.mode == "refuse":
            status = 400
            body = {"error": {"message": REFUSAL}}
        else:
            status = 200
            item = {"url": OUTPUT_URL} if self.mode == "url" else {"b64_json": OUTPUT_B64}
            body = {"data": [item]}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def _passthrough(ctx, payload, phase):
    """The minimum a script must implement: forward the body, pass the reply on.

    ``startswith`` so one callable serves both the unsuffixed phases of a
    single-call script and the ``request:<stage>`` names a cascade uses.
    """
    return dict(payload)


SCRIPT = CompiledScript(sha256="0" * 64, origin="inline", transform=_passthrough)

STAGED_SCRIPT = CompiledScript(
    sha256="1" * 64,
    origin="inline",
    transform=_passthrough,
    stages=("generate",),
)


@pytest.fixture
def vendor():
    """Starts one vendor per requested mode, tearing all of them down after."""
    started: list[HTTPServer] = []

    def _start(mode: str) -> str:
        handler = type(f"_Vendor_{mode}", (_Vendor,), {"mode": mode})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_port}/v1/images/generations"

    yield _start

    for server in started:
        server.shutdown()
        server.server_close()


@pytest.fixture
def settings() -> Settings:
    # Every field that decides what these tests exercise is stated explicitly:
    # Settings reads .env, so a local override would otherwise change the
    # behaviour under test rather than a test failing visibly.
    return Settings(
        environment="dev",
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        fal_key="",
    )


@pytest.fixture
def spans() -> TestExporter:
    """Points Logfire at an in-memory exporter and hands back the exporter."""
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


async def _run(settings: Settings, url: str, payload: dict, script, staged=False) -> str:
    """One request through the real engine, as an outcome string."""
    channel = ChannelSpec(upstream_url=url)
    ctx = AdapterContext(request_id="ctx-test", channel=channel, settings=settings)
    try:
        if staged:
            await execute_staged(ctx, script, channel, settings, payload)
        else:
            await execute(ctx, script, channel, settings, payload)
        return "ok"
    except AdapterError as exc:
        return f"err:{exc.code}"
    finally:
        await ctx.close()


def _attributes(exporter: TestExporter, name: str = "adapt") -> dict:
    logfire.force_flush()
    found = [
        span
        for span in exporter.exported_spans_as_dict()
        if span["name"] == name
    ]
    assert len(found) == 1, f"expected one {name} span, got {len(found)}"
    return found[0]["attributes"]


def _urls(attributes: dict, name: str) -> list[str]:
    """A list-valued attribute, back out of Logfire's own serialisation.

    Logfire renders a non-primitive argument as JSON text and records its
    intended shape in a companion ``logfire.json_schema`` attribute, which is
    what the UI reads to display it as an array. On the raw span -- what a
    non-Logfire exporter or a filter sees -- it is therefore a string, so the
    assertion parses it rather than comparing against a Python list.
    """
    return json.loads(attributes[name])


# --- the request side, on the path that matters ---------------------------


async def test_a_refused_request_leaves_its_prompt_and_reference_in_the_trace(
    vendor, settings, spans
):
    payload = {"prompt": PROMPT, "image": IMAGE_URL}

    assert await _run(settings, vendor("refuse"), payload, SCRIPT) == (
        "err:upstream_http_error"
    )

    attributes = _attributes(spans)
    assert attributes["prompt"] == PROMPT
    assert attributes["prompt_chars"] == len(PROMPT)
    assert _urls(attributes, "image_urls") == [IMAGE_URL]


async def test_an_inline_reference_is_counted_and_the_base64_stays_out(
    vendor, settings, spans
):
    """Every multipart upload arrives as a data URI, so this is the normal case."""
    payload = {"prompt": PROMPT, "image": INLINE_IMAGE}

    assert await _run(settings, vendor("refuse"), payload, SCRIPT) == (
        "err:upstream_http_error"
    )

    attributes = _attributes(spans)
    assert attributes["image_inline_refs"] == 1
    assert "image_urls" not in attributes
    assert not any("AAAA" in str(value) for value in attributes.values())


async def test_the_client_model_is_reported(vendor, settings, spans):
    """It is what the caller asked for; which model actually ran is the
    script's business and deliberately not claimed here."""
    payload = {"prompt": PROMPT, "model": "seedream-3.0"}

    assert await _run(settings, vendor("refuse"), payload, SCRIPT) == (
        "err:upstream_http_error"
    )

    assert _attributes(spans)["model"] == "seedream-3.0"


async def test_a_refused_span_still_identifies_itself_as_an_image_generation(
    vendor, settings, spans
):
    """The identity fields go on in the constructor as well, which is where the
    conventions want them -- and where a failure cannot take them away."""
    payload = {"prompt": PROMPT, "model": "seedream-3.0"}

    assert await _run(settings, vendor("refuse"), payload, SCRIPT) == (
        "err:upstream_http_error"
    )

    attributes = _attributes(spans)
    assert attributes["gen_ai.operation.name"] == "image_generation"
    assert attributes["gen_ai.request.model"] == "seedream-3.0"
    # No provider is claimed: the vocabulary has no value for this channel, and
    # a guessed one would read as fact in the trace.
    assert "gen_ai.provider.name" not in attributes


async def test_a_failing_cascade_carries_the_same_context(vendor, settings, spans):
    """The cascade has no ``adapt`` span, so it needs the context of its own.

    Without it, a staged channel would be the only one whose failures leave no
    prompt in the trace.
    """
    payload = {"prompt": PROMPT, "image": IMAGE_URL}

    assert await _run(
        settings, vendor("refuse"), payload, STAGED_SCRIPT, staged=True
    ) == "err:upstream_http_error"

    attributes = _attributes(spans, "cascade")
    assert attributes["prompt"] == PROMPT
    assert _urls(attributes, "image_urls") == [IMAGE_URL]
    assert attributes["gen_ai.operation.name"] == "image_generation"


# --- the reply side -------------------------------------------------------


async def test_the_finished_image_link_lands_on_the_span(vendor, settings, spans):
    assert await _run(settings, vendor("url"), {"prompt": PROMPT}, SCRIPT) == "ok"

    assert _urls(_attributes(spans), "result_urls") == [OUTPUT_URL]


async def test_a_base64_reply_reports_the_count_and_no_link(vendor, settings, spans):
    """No link was produced, and the trace says which of the two reasons it
    was: nothing came back (attribute absent) or bytes came back."""
    assert await _run(settings, vendor("b64"), {"prompt": PROMPT}, SCRIPT) == "ok"

    attributes = _attributes(spans)
    assert "result_urls" not in attributes
    assert attributes["result_inline_refs"] == 1


async def test_a_refused_request_has_no_result_attributes(vendor, settings, spans):
    """Nothing was produced, so nothing is claimed about a result."""
    payload = {"prompt": PROMPT}

    assert await _run(settings, vendor("refuse"), payload, SCRIPT) == (
        "err:upstream_http_error"
    )

    attributes = _attributes(spans)
    assert "result_urls" not in attributes
    assert "result_inline_refs" not in attributes
