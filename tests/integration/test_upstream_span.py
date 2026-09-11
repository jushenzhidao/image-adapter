"""The ``upstream_call`` span is a published contract, so the suite pins it.

``docs/03_技术架构.md`` §5.2 tells whoever is debugging an incident to read
this span as ``upstream_call (method, url, timeout, status)``. Nothing asserted
that until now: the status code -- the one attribute that separates "the vendor
said no" from "we never got an answer" -- could be dropped by any refactor
without a single test going red.

Driven at ``_do_upstream`` rather than through the app on purpose. The app's
lifespan reconfigures Logfire (``adapter.logfire_setup.init_logfire``), which
would detach the exporter these assertions read from, and the function under
test is the one that owns the span anyway. The upstream is a real socket, so
the transport is exercised rather than replaced by a stand-in.

Out of scope here: the ``adapt`` / ``stage`` / ``script_phase`` spans, which
live in the app-level layers this test deliberately does not enter.
"""

from __future__ import annotations

import json
import socket
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
from adapter.executor import _do_upstream
from adapter.settings import Settings

ADAPTER_KEY = "test-adapter-key"

# Small enough that a reply declaring more than this is refused before any body
# byte is read; the production default is 64 MB (see executor._do_upstream).
BODY_LIMIT = 4096
OVERSIZE = BODY_LIMIT * 2
BODY = json.dumps({"ok": True}).encode()


class _Vendor(BaseHTTPRequestHandler):
    """Replies according to the mode the test asked the fixture for."""

    mode = "ok"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))

        if self.mode == "declared":
            # A failing status *and* an oversized declared length: the reply is
            # refused while reading, after the status line has already arrived.
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(OVERSIZE + 1))
            self.end_headers()
            return

        self.send_response(503 if self.mode == "fail" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    """Starts one vendor per requested mode, tearing all of them down after."""
    started: list[HTTPServer] = []

    def _start(mode: str) -> str:
        handler = type(f"_Vendor_{mode}", (_Vendor,), {"mode": mode})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return f"http://127.0.0.1:{server.server_port}/v1/gen"

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
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        allow_inline_script=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        fal_key="",
        max_upstream_bytes=BODY_LIMIT,
    )


@pytest.fixture
def spans() -> TestExporter:
    """Points Logfire at an in-memory exporter and hands back the exporter.

    The equivalent of Logfire's own ``capfire`` fixture, spelled out here
    because this repo has no shared Logfire fixture to hang it on and a
    module-local one keeps the plugin's fixture out of the test signature.
    """
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


async def _generate(settings: Settings, url: str) -> str:
    """One outbound call through the real transport, as an outcome string."""
    channel = ChannelSpec(upstream_url=url)
    ctx = AdapterContext(request_id="span-test", channel=channel, settings=settings)
    try:
        reply = await _do_upstream(ctx, channel, settings, {"prompt": "cat"})
        return f"ok:{reply.status}"
    except AdapterError as exc:
        return f"err:{exc.code}"
    finally:
        await ctx.close()


def _attributes(exporter: TestExporter, url: str) -> dict:
    """The attributes of the single ``upstream_call`` span for one URL."""
    logfire.force_flush()
    found = [
        span
        for span in exporter.exported_spans_as_dict()
        if span["name"] == "upstream_call" and span["attributes"].get("url") == url
    ]
    assert len(found) == 1, f"expected one upstream_call span, got {len(found)}"
    return found[0]["attributes"]


async def test_a_successful_call_reports_the_published_attributes(
    vendor, settings, spans
):
    url = vendor("ok")
    assert await _generate(settings, url) == "ok:200"

    attributes = _attributes(spans, url)
    assert attributes["method"] == "POST"
    assert attributes["url"] == url
    assert attributes["timeout"] == settings.upstream_timeout
    assert attributes["status"] == 200


async def test_a_rejected_reply_carries_the_vendor_status_code(vendor, settings, spans):
    """A 5xx must be attributable from the trace alone.

    The call raises, so the only place the vendor's own status code survives is
    this attribute -- the error envelope collapses every 4xx/5xx into one code.
    """
    url = vendor("fail")
    assert await _generate(settings, url) == "err:upstream_http_error"

    assert _attributes(spans, url)["status"] == 503


async def test_the_status_is_captured_before_the_body_is_read(vendor, settings, spans):
    """Pins the ordering inside ``_do_upstream``.

    Status line and headers are known the moment the reply starts, while the
    body is read under the ``max_upstream_bytes`` cap. Reading the body first
    -- the previous arrangement -- meant the largest replies, the ones most
    worth attributing, were the ones that lost their status code.
    """
    url = vendor("declared")
    assert await _generate(settings, url) == "err:upstream_body_too_large"

    assert _attributes(spans, url)["status"] == 503


async def test_an_unreachable_vendor_reports_no_status(settings, spans):
    """No status line arrived, so none is claimed.

    An absent attribute reads as "never answered"; a synthesised 0 or 502 would
    be indistinguishable from a real reply in the trace.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1/gen"

    assert await _generate(settings, url) == "err:upstream_unreachable"

    attributes = _attributes(spans, url)
    assert "status" not in attributes
    assert attributes["url"] == url
