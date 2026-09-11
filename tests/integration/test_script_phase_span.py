"""The ``script_phase`` span is a published contract, so the suite pins it.

``docs/03_技术架构.md`` §5.2 draws the trace as ``adapt`` -> ``script_phase`` ->
``upstream_call`` and tells whoever is debugging an incident to read the phase
span for ``phase``. Nothing asserted that, and in fact nothing emitted it: the
tree documented a span the code never opened, so a request killed by the 30 s
phase cap reported only the phase it died in -- never how long it ran or whether
the cap was what stopped it. That is the gap this span closes, and the timeout
case is therefore the one pinned first: a passing request is the case that would
still look green with the span half-implemented.

Driven at ``execute`` rather than through the app, for the same reason
``test_upstream_span.py`` drives ``_do_upstream``: the app's lifespan
reconfigures Logfire and would detach the exporter these assertions read from.
The upstream is a real socket, so the transport is exercised rather than
replaced by a stand-in.
"""

from __future__ import annotations

import asyncio
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

PROMPT = "a cat wearing a hat"

#: Short enough that the cap is reached in a test, not in an incident.
PHASE_TIMEOUT = 0.25

#: Far past the cap, so nothing here depends on scheduling luck.
SLEEP = 5.0

BODY = json.dumps({"data": [{"url": "https://cdn.example.test/o.png"}]}).encode()


class _Vendor(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    started: list[HTTPServer] = []

    def _start() -> str:
        server = HTTPServer(("127.0.0.1", 0), _Vendor)
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
        script_timeout=PHASE_TIMEOUT,
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


async def _passthrough(ctx, payload, phase):
    return dict(payload)


async def _request_phase_overruns(ctx, payload, phase):
    """The shape of the incident: the request phase outlives the cap."""
    if phase == "request":
        await asyncio.sleep(SLEEP)
    return dict(payload)


def _refuses(ctx, payload, phase):
    """A script-reported failure: a real outcome, not a defect."""
    if phase == "request":
        ctx.fail("the image is unusable", param="image", code="image_invalid")
    return dict(payload)


def _script(transform) -> CompiledScript:
    # sha256 distinguishes the cache entries; the value itself is never read by
    # the engine, only reported on the span.
    return CompiledScript(
        sha256=format(abs(hash(transform.__name__)), "064x")[:64],
        origin="inline",
        transform=transform,
    )


async def _run(settings: Settings, url: str, script, payload=None) -> str:
    """One request through the real engine, as an outcome string."""
    channel = ChannelSpec(upstream_url=url)
    ctx = AdapterContext(request_id="phase-test", channel=channel, settings=settings)
    try:
        await execute(ctx, script, channel, settings, payload or {"prompt": PROMPT})
        return "ok"
    except AdapterError as exc:
        return f"err:{exc.code}"
    finally:
        await ctx.close()


def _spans(exporter: TestExporter, name: str) -> list[dict]:
    logfire.force_flush()
    return [span for span in exporter.exported_spans_as_dict() if span["name"] == name]


def _phase(exporter: TestExporter, phase: str) -> dict:
    """The one ``script_phase`` span for a given phase name."""
    found = [
        span for span in _spans(exporter, "script_phase")
        if span["attributes"].get("phase") == phase
    ]
    assert len(found) == 1, f"expected one script_phase for {phase!r}, got {len(found)}"
    return found[0]["attributes"]


def _json_attr(attributes: dict, name: str) -> dict:
    """A dict-valued attribute, back out of Logfire's own serialisation.

    Logfire renders a non-primitive argument as JSON text and records its
    intended shape in a companion ``logfire.json_schema`` attribute, which is
    what the UI reads. On the raw span -- what a non-Logfire exporter sees --
    it is therefore a string, so the assertion parses rather than comparing
    against a Python dict.
    """
    return json.loads(attributes[name])


# --- the case the span exists for -----------------------------------------


async def test_a_phase_cut_off_by_the_cap_says_so(vendor, settings, spans):
    """The whole point: ``outcome`` separates "ran long" from "was stopped".

    Without it, a request that hit the cap and one that finished a hair under it
    are the same span, and the 504 naming the phase is all an operator has.
    """
    assert await _run(settings, vendor(), _script(_request_phase_overruns)) == (
        "err:script_timeout"
    )

    attributes = _phase(spans, "request")
    assert attributes["outcome"] == "timeout"
    assert attributes["error_code"] == "script_timeout"
    assert attributes["timeout"] == PHASE_TIMEOUT
    # The elapsed clock reached the cap rather than the script's own duration:
    # this is what tells a reader the request was waiting, not computing.
    assert attributes["elapsed_ms"] >= PHASE_TIMEOUT * 1000


async def test_a_finished_phase_reports_ok_and_no_error_code(vendor, settings, spans):
    assert await _run(settings, vendor(), _script(_passthrough)) == "ok"

    attributes = _phase(spans, "request")
    assert attributes["outcome"] == "ok"
    assert "error_code" not in attributes


async def test_a_script_reported_failure_keeps_its_own_code(vendor, settings, spans):
    """A ``ctx.fail`` is a client-visible outcome, so the span names which one
    instead of collapsing it into a generic error."""
    assert await _run(settings, vendor(), _script(_refuses)) == "err:image_invalid"

    attributes = _phase(spans, "request")
    assert attributes["outcome"] == "error"
    assert attributes["error_code"] == "image_invalid"


# --- the documented shape -------------------------------------------------


async def test_every_phase_hangs_off_the_request_span(vendor, settings, spans):
    """§5.2 draws the phases *under* ``adapt``; a span reparented to the root
    still exports, and would still pass every attribute assertion above."""
    assert await _run(settings, vendor(), _script(_passthrough)) == "ok"

    request_span = _spans(spans, "adapt")[0]
    children = _spans(spans, "script_phase")
    assert children, "no script_phase span was emitted at all"
    for child in children:
        assert child["parent"]["span_id"] == request_span["context"]["span_id"]


async def test_the_request_span_carries_the_per_phase_breakdown(
    vendor, settings, spans
):
    """One attribute answers "how much of this request was the script at all",
    which the phase spans cannot total by themselves."""
    assert await _run(settings, vendor(), _script(_passthrough)) == "ok"

    attributes = _spans(spans, "adapt")[0]["attributes"]
    phase_ms = _json_attr(attributes, "phase_ms")
    calls = _json_attr(attributes, "phase_calls")
    # The two phases a single-call script always runs, and nothing invented.
    assert sorted(phase_ms) == ["request", "response"]
    assert calls == {"request": 1, "response": 1}
    assert all(value >= 0 for value in phase_ms.values())


# --- the neighbouring contract that had the same hole ---------------------


async def test_the_upstream_call_reports_its_size_and_duration(vendor, settings, spans):
    """``status`` alone said whether the vendor answered, not how big the answer
    was or how long it took to arrive."""
    assert await _run(settings, vendor(), _script(_passthrough)) == "ok"

    attributes = _spans(spans, "upstream_call")[0]["attributes"]
    assert attributes["status"] == 200
    assert attributes["response_bytes"] == len(BODY)
    assert attributes["elapsed_ms"] >= 0
