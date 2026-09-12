"""A degraded ref must reach the trace, not only a log line.

A channel header pointing at a retired version still produces a picture -- from a
different revision of the script than it asked for. The response says so only
through ``X-Script-Sha256``, which needs the manifest to interpret, so the rule
recorded here is that the degradation also gets an event of its own.

Driven at ``resolve_source`` rather than through the app, for the same reason
``test_action_timeouts`` drives ``execute``: the app's lifespan reconfigures
Logfire and would detach the exporter these assertions read from.

The two tests are a pair on purpose. One alone would pass for a recorder that
fires on every request.
"""

from __future__ import annotations

import json

import logfire
import pytest
from logfire.testing import SimpleSpanProcessor, TestExporter

from adapter.channel import ChannelSpec
from adapter.script_source import resolve_source
from adapter.settings import Settings

SCRIPT = "async def transform(ctx, payload, phase):\n    return payload\n"


@pytest.fixture
def spans():
    """A Logfire exporter this file can read, in the repo's own harness style."""
    exporter = TestExporter()
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    return exporter


def _fallback_events(exporter: TestExporter) -> list[dict]:
    logfire.force_flush()
    return [
        span["attributes"]
        for span in exporter.exported_spans_as_dict()
        if span["name"] == "script_ref_fallback"
    ]


def _store(tmp_path, *, retired: bool) -> str:
    """A one-version store; ``retired`` decides whether v2 is gone."""
    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "mj@v1.py").write_text(SCRIPT)
    if not retired:
        (tmp_path / "v" / "mj@v2.py").write_text(SCRIPT)
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {"scripts": {"v/mj": {"latest": "v1", "aliases": {"stable": "v1"}}}}
        )
    )
    return str(tmp_path)


def _spec() -> ChannelSpec:
    return ChannelSpec(
        upstream_url="https://vendor.test/api", script_ref="v/mj@v2"
    )


async def test_a_retired_ref_is_reported_on_the_trace(tmp_path, spans):
    settings = Settings(script_ref_dir=_store(tmp_path, retired=True))
    source = await resolve_source(_spec(), settings)

    assert source.fallback_to == "v/mj@stable", "the source must carry the deviation"
    events = _fallback_events(spans)
    assert len(events) == 1
    assert events[0]["requested"] == "v/mj@v2"
    assert events[0]["serving"] == "v/mj@stable"
    # The digest is how a reader ties the event to the script that actually ran.
    assert events[0]["script_sha256"] == source.sha256[:12]


async def test_a_ref_that_resolves_is_not_reported(tmp_path, spans):
    settings = Settings(script_ref_dir=_store(tmp_path, retired=False))
    source = await resolve_source(_spec(), settings)

    assert source.fallback_to is None
    assert _fallback_events(spans) == []
