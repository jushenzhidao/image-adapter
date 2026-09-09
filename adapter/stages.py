"""Multi-stage cascade driver (v1.1, AC-24 / AC-26 / AC-28 / AC-31).

`executor.execute()` describes exactly one upstream call: auth, request, call,
response. A cascade -- preprocess then generate then upscale -- needs N calls
where each one's input is the previous one's output, which that fixed chain
cannot express.

Rather than add a second execution path, this module wraps the existing one in
a loop. A script opts in with a module-level declaration:

    STAGES = ["generate", "upscale"]
    STAGE_FALLBACK = ["upscale"]

and the phase names it receives gain a suffix: `request:generate`,
`response:generate`, `request:upscale`, and so on. Scripts that declare no
STAGES see unsuffixed phases and the original single-call path, so nothing
about their behaviour changes.

The primitives are reused verbatim: `_call_phase`, `_do_upstream` and
`_run_poll_loop` all come from the executor. This module only sequences them,
carries artefacts between stages, and decides what a failure means.
"""

from __future__ import annotations

from typing import Any

import logfire

from adapter.budget import Budget, resolve_total
from adapter.channel import ChannelSpec
from adapter.context import SKIP, AdapterContext, RequestPlan
from adapter.errors import AdapterError, ChannelConfigError, ScriptRuntimeError
from adapter.executor import (
    PHASE_AUTH,
    PHASE_POLL_REQUEST,
    PHASE_POLL_RESPONSE,
    _call_phase,
    _do_upstream,
    _run_poll_loop,
)
from adapter.script_cache import PHASE_DEGRADED, CompiledScript
from adapter.settings import Settings


class StageOutcome:
    """What a cascade produced, plus whether it had to degrade to get there."""

    __slots__ = ("payload", "degraded_stage", "degraded_reason", "calls")

    def __init__(
        self,
        payload: Any,
        degraded_stage: str | None = None,
        degraded_reason: str | None = None,
        calls: int = 0,
    ) -> None:
        self.payload = payload
        self.degraded_stage = degraded_stage
        self.degraded_reason = degraded_reason
        self.calls = calls

    @property
    def degraded(self) -> bool:
        return self.degraded_stage is not None

    @property
    def headers(self) -> dict[str, str]:
        """Marks a degraded result so a 200 is not mistaken for a full one."""
        if not self.degraded:
            return {}
        return {
            "X-Adapter-Degraded": self.degraded_stage or "",
            "X-Adapter-Degraded-Reason": (self.degraded_reason or "")[:200],
        }


def resolve_stages(
    script: CompiledScript, channel: ChannelSpec, settings: Settings
) -> tuple[str, ...]:
    """Decides the stage list: the channel header wins over the script.

    The control plane may reorder or trim stages for a given channel, but it
    cannot invent one the script has no code for.
    """
    override = channel.stages.names
    if not override:
        stages = script.stages
    else:
        if not script.staged:
            raise ChannelConfigError(
                "X-Stages was sent but the script declares no STAGES",
                "X-Stages",
            )
        unknown = sorted(set(override) - set(script.stages))
        if unknown:
            raise ChannelConfigError(
                f"X-Stages names stages the script does not declare: {unknown}",
                "X-Stages",
            )
        stages = override
    if len(stages) > settings.stage_max_count:
        raise ChannelConfigError(
            f"Cascade declares {len(stages)} stages, over the "
            f"{settings.stage_max_count} stage limit",
            "X-Stages",
        )
    return stages


async def _run_stage(
    script: CompiledScript,
    ctx: AdapterContext,
    channel: ChannelSpec,
    settings: Settings,
    payload: Any,
    stage: str,
    budget: Budget,
) -> tuple[Any, bool]:
    """Runs one stage. Returns (artefact, ran) -- ran=False means skipped."""
    # Each stage starts from a clean plan so one stage's emitted URL, method
    # or timeout cannot leak into the next.
    ctx.plan = RequestPlan()

    body = await _call_phase(
        script, ctx, payload, f"request:{stage}", settings.script_timeout
    )
    if body is SKIP:
        # AC-31: no upstream call, no artefact, no budget spent.
        return None, False

    # A stage URL from the control plane is the default target; the script can
    # still override it via ctx.emit(url=...), which re-runs the SSRF check.
    default_url = channel.stages.urls.get(stage)

    reply = await _do_upstream(
        ctx,
        channel,
        settings,
        body,
        default_url=default_url,
        budget=budget,
        stage=stage,
    )

    if channel.async_spec.enabled:
        missing = [
            p
            for p in (PHASE_POLL_REQUEST, PHASE_POLL_RESPONSE)
            if not script.handles(p)
        ]
        if missing:
            raise ChannelConfigError(
                "X-Async is set but the script does not declare "
                f"PHASES for {missing}",
                "X-Async",
            )
        upstream_payload = await _run_poll_loop(
            script, ctx, channel, settings, reply, budget=budget, stage=stage
        )
    else:
        upstream_payload = reply.payload

    # The response phase gets a fresh plan too: it may emit for the next stage.
    ctx.plan = RequestPlan()
    out = await _call_phase(
        script,
        ctx,
        upstream_payload,
        f"response:{stage}",
        settings.script_timeout,
    )
    return out, True


async def execute_staged(
    ctx: AdapterContext,
    script: CompiledScript,
    channel: ChannelSpec,
    settings: Settings,
    client_payload: dict,
) -> StageOutcome:
    """Drives the declared stages in order and returns the final artefact."""
    stages = resolve_stages(script, channel, settings)
    budget = Budget(
        total=resolve_total(
            channel.stages.budget,
            settings.stage_budget_default,
            settings.stage_budget_max,
        )
    )
    # Scripts read the countdown through ctx.remaining / ctx.deadline.
    ctx._budget = budget
    ctx.stage = {}

    outcome = StageOutcome(payload=None)
    payload: Any = client_payload
    last_artefact: Any = None

    with logfire.span(
        "cascade",
        endpoint=ctx.endpoint,
        script_sha256=script.sha256[:12],
        stages=list(stages),
        budget_s=budget.total,
    ):
        # The auth phase is cascade-wide, not per stage: one credential serves
        # every call, and the headers it emits survive each stage's plan reset
        # because the engine re-applies them on every outbound call.
        auth_headers: dict[str, str] = {}
        if script.handles(PHASE_AUTH):
            emitted = await _call_phase(
                script, ctx, client_payload, PHASE_AUTH, settings.script_timeout
            )
            if isinstance(emitted, dict):
                auth_headers = {str(k): str(v) for k, v in emitted.items()}

        for stage in stages:
            with logfire.span(
                "stage", name=stage, remaining_s=budget.remaining
            ) as span:
                if auth_headers:
                    ctx.plan = RequestPlan()
                    ctx.emit(headers=auth_headers)
                try:
                    artefact, ran = await _run_stage(
                        script, ctx, channel, settings, payload, stage, budget
                    )
                except AdapterError as exc:
                    # A post-processing stage that fails is recoverable as long
                    # as an earlier stage already produced something usable:
                    # returning the un-upscaled image beats returning nothing.
                    if not (script.may_degrade(stage) and last_artefact is not None):
                        raise
                    span.set_attribute("degraded", True)
                    span.set_attribute("degraded_reason", exc.code or "error")
                    logfire.warn(
                        "stage_degraded",
                        stage=stage,
                        reason=exc.code,
                        message=exc.message,
                    )
                    outcome.degraded_stage = stage
                    outcome.degraded_reason = exc.code or "error"
                    break

                if not ran:
                    span.set_attribute("skipped", True)
                    continue

                outcome.calls += 1
                span.set_attribute("upstream_calls", outcome.calls)
                ctx.stage[stage] = artefact
                last_artefact = artefact
                payload = artefact

    if last_artefact is None:
        raise ScriptRuntimeError(
            f"request:{stages[-1]}" if stages else "request",
            "every stage was skipped, so there is no result to return",
        )

    if outcome.degraded and script.handles(PHASE_DEGRADED):
        # The artefact in hand is an inter-stage handoff, not a client
        # response. Give the script a chance to shape the partial result.
        ctx.plan = RequestPlan()
        last_artefact = await _call_phase(
            script, ctx, last_artefact, PHASE_DEGRADED, settings.script_timeout
        )

    if not isinstance(last_artefact, (dict, list)):
        raise ScriptRuntimeError(
            f"response:{outcome.degraded_stage or stages[-1]}",
            "must return a JSON object or array",
        )

    outcome.payload = last_artefact
    return outcome
