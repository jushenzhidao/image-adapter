"""One request path shared by every OpenAI-compatible endpoint.

The endpoint modules differ only in their route and a light payload check:
adaptation itself is endpoint-agnostic because the channel supplies both the
upstream URL and the script.
"""

from __future__ import annotations

import hmac
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from starlette.requests import Request
from starlette.responses import JSONResponse

from adapter.api.common import get_request_id, parse_json_body
from adapter.channel import parse_channel
from adapter.context import AdapterContext
from adapter.errors import AdmissionError, ScriptSourceError
from adapter.executor import execute
from adapter.script_source import resolve_source
from adapter.settings import Settings
from adapter.stages import execute_staged


def check_admission(request: Request, settings: Settings) -> None:
    """Gates the data plane on X-Adapter-Key.

    Inline scripts execute in-process, so an open adapter is an open code
    execution endpoint. Authorization is deliberately not accepted here: it
    belongs to the upstream vendor.
    """
    if not settings.adapter_key_required:
        return
    expected = settings.adapter_key
    if not expected:
        raise AdmissionError(
            "ADAPTER_KEY is not configured; refusing to serve requests"
        )
    supplied = request.headers.get("x-adapter-key", "")
    if not supplied:
        raise AdmissionError()
    if not hmac.compare_digest(supplied, expected):
        raise AdmissionError()


async def _fetch_remote_script(url: str, settings: Settings) -> str:
    timeout = aiohttp.ClientTimeout(total=settings.remote_script_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise ScriptSourceError(
                    f"Script fetch failed with status {resp.status}",
                    code="script_fetch_failed",
                )
            raw = await resp.read()
    if len(raw) > settings.max_script_bytes:
        raise ScriptSourceError(
            "Remote script exceeds the configured size limit",
            code="script_too_large",
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptSourceError("Remote script must be UTF-8 text") from None


@dataclass
class AdaptResult:
    """Raw pipeline output, before it is shaped into an HTTP response."""

    payload: Any
    request_id: str
    script_sha256: str
    stream: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "X-Request-Id": self.request_id,
            "X-Script-Sha256": self.script_sha256,
        }
        # Carries X-Adapter-Degraded when a cascade fell back (AC-28).
        headers.update(self.extra_headers)
        return headers


async def adapt(
    request: Request,
    endpoint: str,
    prepare: Callable[[dict], Any] | None = None,
    body: dict | None = None,
) -> AdaptResult:
    """Admission -> prepare -> channel parse -> script load -> execute.

    `prepare` validates and may enrich the client payload in place. It can be
    sync or async, since some endpoints need to load prior state first.

    `body` lets a route supply an already-decoded payload instead of reading
    JSON off the wire. /v1/images/edits uses it to hand over the multipart
    form after normalising it, so both image routes share one pipeline.
    """
    settings: Settings = request.app.state.settings
    check_admission(request, settings)

    request_id = get_request_id(request)
    payload = body if body is not None else await parse_json_body(request)
    if prepare is not None:
        outcome = prepare(payload)
        if inspect.isawaitable(outcome):
            await outcome
    channel = parse_channel(request.headers, settings)

    async def fetch(url: str) -> str:
        return await _fetch_remote_script(url, settings)

    source = await resolve_source(channel, settings, fetch=fetch)
    script = request.app.state.script_cache.load(source)

    ctx = AdapterContext(
        request_id=request_id,
        channel=channel,
        settings=settings,
        endpoint=endpoint,
        http=getattr(request.app.state, "http", None),
        cache=getattr(request.app.state, "asset_cache", None),
    )
    extra_headers: dict[str, str] = {}
    try:
        if script.staged or channel.stages.names:
            outcome = await execute_staged(ctx, script, channel, settings, payload)
            result = outcome.payload
            extra_headers = outcome.headers
        else:
            result = await execute(ctx, script, channel, settings, payload)
    finally:
        await ctx.close()

    return AdaptResult(
        payload=result,
        request_id=request_id,
        script_sha256=script.sha256,
        stream=bool(payload.get("stream", False)),
        extra_headers=extra_headers,
    )


async def run_endpoint(
    request: Request,
    endpoint: str,
    prepare: Callable[[dict], Any] | None = None,
) -> JSONResponse:
    """The common case: adapt and return the result as JSON."""
    outcome = await adapt(request, endpoint, prepare)
    return JSONResponse(outcome.payload, headers=outcome.headers)
