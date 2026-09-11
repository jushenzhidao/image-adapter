"""The execution engine: one channel + one script + one client payload.

Pipeline, all driven by the single script entry point
transform(ctx, payload, phase):

  auth            optional. Returns credential material; skipped when absent.
  request         required. Client payload -> upstream payload.
  <upstream call> engine-owned: URL, credential emission, timeout, errors.
  poll_request    optional, async channels only. Job handle -> poll call.
  poll_response   optional, async channels only. Decides done/pending.
  response        required. Upstream payload -> OpenAI-shaped result.

The engine never inspects vendor semantics; the script never touches sockets.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import aiohttp
import logfire

from adapter.budget import Budget
from adapter.channel import ChannelSpec
from adapter.context import AdapterContext
from adapter.errors import (
    AdapterError,
    ChannelConfigError,
    PipelineTimeoutError,
    PollTimeoutError,
    ScriptRuntimeError,
    ScriptTimeoutError,
    StageTimeoutError,
    UpstreamError,
)
from adapter.script_cache import CompiledScript
from adapter.settings import Settings
from adapter.trace_attrs import (
    phase_summary,
    record_result,
    span_elapsed_ms,
    summarise_request,
)
from adapter.transport import (
    UpstreamReply,
    build_request,
    parse_body,
    raise_for_status,
    read_capped,
)

PHASE_AUTH = "auth"
PHASE_REQUEST = "request"
PHASE_RESPONSE = "response"
PHASE_POLL_REQUEST = "poll_request"
PHASE_POLL_RESPONSE = "poll_response"


def _note_phase(
    span: Any,
    ctx: AdapterContext,
    phase: str,
    started: float,
    outcome: str,
    error_code: str | None,
) -> None:
    """Closes the books on one phase: the span, then the request's tally.

    ``outcome`` is what separates "the script ran for 28 s" from "the cap cut it
    off at 30 s", which the error code alone cannot say -- both end as a 504
    naming the phase.
    """
    elapsed_ms = (time.monotonic() - started) * 1000.0
    span.set_attribute("elapsed_ms", round(elapsed_ms, 1))
    span.set_attribute("outcome", outcome)
    if error_code is not None:
        span.set_attribute("error_code", error_code)
    ctx.record_phase(phase, elapsed_ms)


async def _call_phase(
    script: CompiledScript,
    ctx: AdapterContext,
    payload: Any,
    phase: str,
    timeout: float,
) -> Any:
    """Runs one phase with a wall-clock cap, normalizing script failures.

    Publishes the ``script_phase`` span that docs/03 §5.2 already promises. The
    timeout error names the phase but never says how long it ran or whether the
    cap is what stopped it, and with several phases in one request (a cascade
    multiplies them) that gap is the difference between reading a trace and
    guessing at one.
    """
    started = time.monotonic()
    outcome = "ok"
    error_code: str | None = None

    with logfire.span("script_phase", phase=phase, timeout=timeout) as span:
        try:
            result = script.transform(ctx, payload, phase)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
        except TimeoutError:
            outcome, error_code = "timeout", "script_timeout"
            raise ScriptTimeoutError(phase, timeout) from None
        except AdapterError as exc:
            # A script-reported failure is a real outcome, not a defect: record
            # which one so the span distinguishes a 400 from a crash.
            outcome, error_code = "error", exc.code
            raise
        except Exception as exc:
            outcome, error_code = "error", type(exc).__name__
            raise ScriptRuntimeError(phase, type(exc).__name__) from exc
        finally:
            _note_phase(span, ctx, phase, started, outcome, error_code)
        return result


def _phase_supported(script: CompiledScript, phase: str) -> bool:
    """Optional phases are opt-in through a module-level PHASES declaration."""
    return script.handles(phase)


async def _do_upstream(
    ctx: AdapterContext,
    channel: ChannelSpec,
    settings: Settings,
    body: Any,
    *,
    default_url: str | None = None,
    budget: Budget | None = None,
    stage: str | None = None,
) -> UpstreamReply:
    """Performs the outbound call described by ctx.plan plus the channel.

    `budget`, when given, is the cascade-wide countdown: the call is clamped to
    what remains, so N stages cannot each spend a full per-stage timeout.
    """
    if budget is not None and budget.exhausted:
        raise PipelineTimeoutError(budget.total, stage)

    url, method, kwargs = build_request(channel, ctx.plan, body, default_url)

    timeout = ctx.plan.timeout or settings.upstream_timeout
    if budget is not None:
        timeout = budget.cap(timeout)
    kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)

    with logfire.span(
        "upstream_call", method=method, url=url, timeout=timeout
    ) as span, span_elapsed_ms(span):
        try:
            async with ctx.http.request(method, url, **kwargs) as resp:
                # Status and headers are available as soon as the response
                # line is in, so they are captured before the body: a reply we
                # then refuse to read in full is still attributed to its code.
                status = resp.status
                content_type = resp.headers.get("Content-Type", "")
                reply_headers = dict(resp.headers)
                span.set_attribute("status", status)

                # Bounded read: the whole body is buffered, so an unbounded one
                # is a direct path into this worker's memory.
                raw = await read_capped(
                    resp,
                    settings.max_upstream_bytes,
                    lambda detail: UpstreamError(
                        detail, code="upstream_body_too_large", status=502
                    ),
                    label="Upstream response",
                )
                # Set only once the body was actually read, on the same rule as
                # `status`: an absent size says "we never got the bytes", which
                # must stay distinguishable from a genuinely empty reply.
                span.set_attribute("response_bytes", len(raw))
        except TimeoutError:
            # Which limit actually bit matters to the caller: an exhausted
            # budget means the whole request is over, while a stage timeout
            # leaves the cascade's remaining stages viable in principle.
            if budget is not None and budget.exhausted:
                raise PipelineTimeoutError(budget.total, stage, during=True) from None
            if stage is not None:
                raise StageTimeoutError(stage, timeout) from None
            raise UpstreamError(
                f"Upstream did not respond within {timeout:.0f}s",
                code="upstream_timeout",
                status=504,
            ) from None
        except aiohttp.ClientError as exc:
            raise UpstreamError(
                "Could not reach the upstream endpoint", code="upstream_unreachable"
            ) from exc

    parsed = parse_body(raw, content_type)
    raise_for_status(status, parsed)

    return UpstreamReply(
        status=status,
        json=parsed,
        raw=None if parsed is not None else raw,
        headers=reply_headers,
    )


async def _run_poll_loop(
    script: CompiledScript,
    ctx: AdapterContext,
    channel: ChannelSpec,
    settings: Settings,
    first: UpstreamReply,
    *,
    budget: Budget | None = None,
    stage: str | None = None,
) -> Any:
    """Drives poll_request / poll_response until the job finishes."""
    spec = channel.async_spec
    deadline = time.monotonic() + spec.timeout
    handle: Any = first.payload
    attempts = 0

    while True:
        if time.monotonic() >= deadline:
            raise PollTimeoutError(spec.timeout)
        if budget is not None and budget.exhausted:
            raise PipelineTimeoutError(budget.total, stage)
        attempts += 1
        if attempts > settings.poll_max_attempts:
            raise PollTimeoutError(spec.timeout)

        # Never sleep past the budget: waiting out an interval we cannot
        # afford would turn a clean 504 into a late one.
        interval = spec.poll_interval
        if budget is not None:
            interval = budget.cap(interval)
        await asyncio.sleep(interval)

        ctx.reset_plan()
        poll_body = await _call_phase(
            script, ctx, handle, PHASE_POLL_REQUEST, settings.script_timeout
        )
        reply = await _do_upstream(
            ctx, channel, settings, poll_body, budget=budget, stage=stage
        )

        verdict = await _call_phase(
            script, ctx, reply.payload, PHASE_POLL_RESPONSE, settings.script_timeout
        )
        if not isinstance(verdict, dict):
            raise ScriptRuntimeError(
                PHASE_POLL_RESPONSE, "must return a dict with a 'done' key"
            )
        if verdict.get("error"):
            raise UpstreamError(
                f"Upstream job failed: {str(verdict['error'])[:200]}",
                code="upstream_job_failed",
            )
        if verdict.get("done"):
            return verdict.get("payload", reply.payload)
        handle = verdict.get("payload", handle)


async def execute(
    ctx: AdapterContext,
    script: CompiledScript,
    channel: ChannelSpec,
    settings: Settings,
    client_payload: dict,
    *,
    budget: Budget | None = None,
    stage: str | None = None,
) -> Any:
    """Runs the full adaptation pipeline and returns the client-facing result."""
    # The client's prompt and reference images go on in the constructor, not
    # after the call: an attribute written before a failure survives it, and a
    # vendor refusal is the case where it is needed (see adapter.trace_attrs).
    # Read off the body as the pipeline handed it over, before the request
    # phase runs, so a script that mutates the body in place cannot change
    # what the trace says the client sent.
    with logfire.span(
        "adapt",
        endpoint=ctx.endpoint,
        script_sha256=script.sha256[:12],
        script_origin=script.origin,
        upstream_url=channel.upstream_url,
        is_async=channel.async_spec.enabled,
        stage=stage,
        **summarise_request(client_payload),
    ) as span, phase_summary(span, ctx):
        if _phase_supported(script, PHASE_AUTH):
            emitted = await _call_phase(
                script, ctx, client_payload, PHASE_AUTH, settings.script_timeout
            )
            if isinstance(emitted, dict):
                ctx.emit(headers=emitted)

        # Headers emitted by the auth phase carry into the request phase.
        upstream_body = await _call_phase(
            script, ctx, client_payload, PHASE_REQUEST, settings.script_timeout
        )

        reply = await _do_upstream(
            ctx, channel, settings, upstream_body, budget=budget, stage=stage
        )

        # X-Async is the control plane asserting this upstream is job-based, so
        # the paired script must handle both poll phases.
        payload: Any = reply.payload
        if channel.async_spec.enabled:
            missing = [
                p
                for p in (PHASE_POLL_REQUEST, PHASE_POLL_RESPONSE)
                if not _phase_supported(script, p)
            ]
            if missing:
                raise ChannelConfigError(
                    "X-Async is set but the script does not declare "
                    f"PHASES for {missing}",
                    "X-Async",
                )
            payload = await _run_poll_loop(
                script, ctx, channel, settings, reply, budget=budget, stage=stage
            )

        ctx.reset_plan()
        result = await _call_phase(
            script, ctx, payload, PHASE_RESPONSE, settings.script_timeout
        )
        if not isinstance(result, (dict, list)):
            raise ScriptRuntimeError(
                PHASE_RESPONSE, "must return a JSON object or array"
            )
        # The image links in the reply go on now, while the span is still open:
        # "the request succeeded but where is the picture" is the other half of
        # the incident this context exists for.
        record_result(span, result)
        return result

