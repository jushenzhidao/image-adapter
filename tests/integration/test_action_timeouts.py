"""Each bound must name itself, and none of them may absorb another.

``docs/03_技术架构.md`` §3.3.1 and ``adapter/settings.py`` keep six bounds
separate for one stated reason: which one fired is what the caller sees, so
merging them would make the error code lie (AC-27). Two of those bounds --
``image_download_timeout`` and ``storage_upload_timeout`` -- were added because
``script_timeout`` was covering for them by accident: an unreachable client
image consumed the whole phase budget and then reported ``script_timeout``,
which names the wrong layer and sends the reader to the vendor.

So the assertions here are deliberately paired. A test that only checks the new
code appears would pass even if the phase cap had silently stopped working,
which is precisely how the two ends of this change could go wrong at once.

Driven at ``execute`` rather than through the app, for the same reason
``test_upstream_span.py`` drives ``_do_upstream``: the app's lifespan
reconfigures Logfire and would detach the exporter these assertions read from.
"""

from __future__ import annotations

import asyncio
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
from adapter.executor import execute
from adapter.script_cache import CompiledScript
from adapter.settings import Settings
from adapter.storage.base import StoredObject

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64

DOWNLOAD_BOUND = 0.3
UPLOAD_BOUND = 0.2
PHASE_CAP = 5.0

#: Long enough that a bound firing first is unambiguous, short enough that a
#: bound that failed to fire ends the test rather than hanging CI.
HANG = 30.0

OK_BODY = b'{"data": [{"url": "https://cdn.example.test/o.png"}]}'


class _Vendor(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(OK_BODY)))
        self.end_headers()
        self.wfile.write(OK_BODY)

    def log_message(self, *args):
        pass


class _WedgedStore:
    """Accepts the upload and never answers."""

    name = "wedged"

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        await asyncio.sleep(HANG)
        raise AssertionError("unreachable")


class _HealthyStore:
    name = "healthy"

    async def put(self, data: bytes, *, key: str, content_type: str) -> StoredObject:
        return StoredObject(
            url="https://cdn.example.test/uploaded.png",
            key=key,
            visibility="public",
            expires_at=None,
        )


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
def black_hole():
    """A host that completes the handshake and then answers nothing.

    Never accepted, so the kernel answers the SYN from the backlog and the
    request goes into the void -- which is what a black-holed image host looks
    like from here, and is the shape of the incident this bound exists for.
    """
    servers: list[socket.socket] = []

    def _start() -> str:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        servers.append(server)
        return f"http://127.0.0.1:{server.getsockname()[1]}/a.png"

    yield _start

    for server in servers:
        server.close()


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
        script_timeout=PHASE_CAP,
        image_download_timeout=DOWNLOAD_BOUND,
        storage_upload_timeout=UPLOAD_BOUND,
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


async def _fetch_client_image(ctx, payload, phase):
    """The incident: the request phase fetches the client's own reference."""
    if phase == "request":
        await ctx.download_image(payload["image"])
    return dict(payload)


async def _upload_an_image(ctx, payload, phase):
    """Uploads in the request phase, then hands the value back in the reply.

    The uploaded value has to travel through the response phase to be asserted
    on: the request phase's return value becomes the upstream body and is never
    seen by the caller.
    """
    if phase == "request":
        ctx._uploaded = await ctx.upload_temp_image(PNG, ext="png")
        return dict(payload)
    return {"uploaded": getattr(ctx, "_uploaded", None)}


async def _overrun_the_phase(ctx, payload, phase):
    """The script itself runs long, with no external call involved."""
    if phase == "request":
        await asyncio.sleep(HANG)
    return dict(payload)


def _script(transform) -> CompiledScript:
    return CompiledScript(
        sha256=format(abs(hash(transform.__name__)), "064x")[:64],
        origin="inline",
        transform=transform,
    )


async def _run(settings, url, script, payload, storage=None):
    channel = ChannelSpec(upstream_url=url)
    ctx = AdapterContext(
        request_id="action-bound", channel=channel, settings=settings, storage=storage
    )
    try:
        result = await execute(ctx, script, channel, settings, payload)
        return "ok", result
    except AdapterError as exc:
        return f"err:{exc.code}", None
    finally:
        await ctx.close()


def _spans(exporter: TestExporter, name: str) -> list[dict]:
    logfire.force_flush()
    return [span for span in exporter.exported_spans_as_dict() if span["name"] == name]


# --- the download bound names itself --------------------------------------


async def test_an_unreachable_client_image_names_its_own_bound(
    vendor, black_hole, settings, spans
):
    """It used to report `script_timeout`, which points at the script and hides
    the host. That misattribution is the reason this bound exists."""
    outcome, _ = await _run(
        settings,
        vendor(),
        _script(_fetch_client_image),
        {"prompt": "cat", "image": black_hole()},
    )

    assert outcome == "err:image_download_timeout"


async def test_the_download_bound_is_not_the_phase_bound(vendor, black_hole, settings):
    """Stated as a relationship, not a constant: the download gives up long
    before the phase would, and the two numbers must stay distinct."""
    assert DOWNLOAD_BOUND < PHASE_CAP

    started = asyncio.get_running_loop().time()
    await _run(
        settings,
        vendor(),
        _script(_fetch_client_image),
        {"prompt": "cat", "image": black_hole()},
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < PHASE_CAP, (
        "the request ran to the phase cap, so the download bound did not fire"
    )


async def test_a_slow_download_still_reports_on_its_own_span(
    vendor, black_hole, settings, spans
):
    """The bound and the trace agree on the same code, so an incident can be
    read from either one."""
    await _run(
        settings,
        vendor(),
        _script(_fetch_client_image),
        {"prompt": "cat", "image": black_hole()},
    )

    attributes = _spans(spans, "download_image")[0]["attributes"]
    assert attributes["outcome"] == "error"
    assert attributes["error_code"] == "image_download_timeout"


# --- and the phase cap still works ----------------------------------------


async def test_a_long_running_script_is_still_the_phase_bound(
    vendor, settings, spans
):
    """The other half of AC-27. Narrowing the action bounds must not have
    quietly disarmed the cap that guards the script itself."""
    outcome, _ = await _run(
        settings, vendor(), _script(_overrun_the_phase), {"prompt": "cat"}
    )

    assert outcome == "err:script_timeout"

    attributes = [
        span["attributes"]
        for span in _spans(spans, "script_phase")
        if span["attributes"].get("phase") == "request"
    ][0]
    assert attributes["outcome"] == "timeout"
    assert attributes["timeout"] == PHASE_CAP


# --- the upload bound degrades, and says so -------------------------------


async def test_a_wedged_upload_degrades_instead_of_failing(
    vendor, settings, spans
):
    """The store is an accelerator, so the request must still succeed.

    What changes with the bound is that the answer is a data URI *on purpose*
    rather than because the phase cap happened to kill the request.
    """
    outcome, result = await _run(
        settings,
        vendor(),
        _script(_upload_an_image),
        {"prompt": "cat"},
        storage=_WedgedStore(),
    )

    assert outcome == "ok"
    assert result["uploaded"].startswith("data:image/png;base64,")


async def test_a_wedged_upload_is_attributed_on_its_span(
    vendor, settings, spans
):
    """For the caller the degradation is silent -- they get base64 and a 200.
    The trace is the only place the reason survives, which is why the span
    carries a code of its own rather than the exception's class name."""
    await _run(
        settings,
        vendor(),
        _script(_upload_an_image),
        {"prompt": "cat"},
        storage=_WedgedStore(),
    )

    attributes = _spans(spans, "storage_put")[0]["attributes"]
    assert attributes["store"] == "wedged"
    assert attributes["outcome"] == "error"
    assert attributes["error_code"] == "storage_upload_timeout"


async def test_a_healthy_upload_reports_ok_and_no_error_code(
    vendor, settings, spans
):
    _, result = await _run(
        settings,
        vendor(),
        _script(_upload_an_image),
        {"prompt": "cat"},
        storage=_HealthyStore(),
    )

    assert result["uploaded"] == "https://cdn.example.test/uploaded.png"

    attributes = _spans(spans, "storage_put")[0]["attributes"]
    assert attributes["outcome"] == "ok"
    assert "error_code" not in attributes
