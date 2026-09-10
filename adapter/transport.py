"""Outbound-call plumbing shared by the single-stage and cascade engines.

Split out of executor.py to keep each module within the per-file size budget.
Nothing here knows about phases or stages: it turns a RequestPlan plus a
ChannelSpec into one HTTP call and a normalised reply.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import aiohttp

from adapter.channel import ChannelSpec
from adapter.ctxapi import RequestPlan
from adapter.errors import UpstreamError
from adapter.jsoncodec import loads as _json_loads

JSON_CONTENT = "application/json"

#: Streaming read granularity. 64 KiB keeps the per-iteration cost negligible
#: against the socket read while bounding the transient extra allocation.
READ_CHUNK = 65536


@dataclass
class UpstreamReply:
    """What came back from the vendor, before the response phase runs."""

    status: int
    json: Any = None
    raw: bytes | None = None
    headers: dict[str, str] | None = None

    @property
    def payload(self) -> Any:
        """JSON when the vendor sent JSON, raw bytes when it sent an image."""
        return self.json if self.json is not None else self.raw


def merge_url(base: str, plan: RequestPlan) -> str:
    """Applies an emitted URL override and any emitted query parameters."""
    url = plan.url or base
    if not plan.query:
        return url
    parts = urlparse(url)
    merged = urlencode(plan.query)
    query = f"{parts.query}&{merged}" if parts.query else merged
    return urlunparse(parts._replace(query=query))


def apply_auth(
    channel: ChannelSpec, headers: dict[str, str], body: Any, query: dict[str, str]
) -> None:
    """Puts the upstream credential where X-Auth-Emit says it goes."""
    auth = channel.auth
    key = channel.upstream_key
    if auth.target == "none" or not key:
        return
    value = f"{auth.prefix} {key}".strip() if auth.prefix else key
    if auth.target == "header":
        headers.setdefault(auth.name, value)
    elif auth.target == "query":
        query.setdefault(auth.name, value)
    elif auth.target == "body" and isinstance(body, dict):
        body.setdefault(auth.name, value)


async def read_capped(
    resp: aiohttp.ClientResponse,
    limit: int,
    error: Callable[[str], Exception],
    label: str = "Body",
) -> bytes:
    """Reads a whole body, refusing to buffer more than ``limit`` bytes.

    ``resp.read()`` has no size bound, and the adapter holds one buffer per
    in-flight request, so an oversized -- or hostile -- reply writes straight
    into the process's memory: 1000 in-flight requests at 8 MB each is already
    ~8 GB. This makes the ceiling a property of the request instead of a
    property of whoever is on the other end of the socket.

    ``error`` builds the domain exception, because the callers report an
    overrun differently: an oversized upstream reply is a 502, an oversized
    script fetch is a 400. ``label`` names the thing in the message.

    ``Content-Length`` is consulted first so an honest but oversized reply
    fails before anything is read; the running total is what actually enforces
    the cap, since a chunked reply declares no length at all.
    """
    declared = resp.content_length
    if declared is not None and declared > limit:
        raise error(f"{label} is {declared} bytes, over the {limit} byte limit")
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.content.iter_chunked(READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise error(f"{label} exceeds the {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_body(raw: bytes, content_type: str) -> Any:
    """Decodes a JSON reply, or returns None when the body is not JSON.

    Runs on the event loop, and an upstream image reply is routinely several
    megabytes of JSON, so this goes through the shared codec (see
    ``adapter.jsoncodec``) rather than the stdlib. The decode is skipped too:
    orjson reads bytes directly, where ``json.loads`` needs a str first.
    """
    looks_json = "json" in content_type or (
        raw[:1] in (b"{", b"[") if raw else False
    )
    if not looks_json:
        return None
    try:
        return _json_loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def raise_for_status(status: int, parsed: Any) -> None:
    """Turns a vendor error response into an UpstreamError.

    The vendor's own message is surfaced, truncated: it is the single most
    useful thing for diagnosing a failing channel, but it is untrusted text.
    """
    if status < 400:
        return
    detail = ""
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message", ""))[:200]
        elif isinstance(err, str):
            detail = err[:200]
        elif "message" in parsed:
            detail = str(parsed["message"])[:200]
    message = f"Upstream returned {status}"
    if detail:
        message = f"{message}: {detail}"
    raise UpstreamError(
        message,
        code="upstream_http_error",
        status=502 if status >= 500 else 400,
        upstream_status=status,
    )


def _as_text(value: Any) -> str:
    """One multipart text part. Booleans must read ``true``, not ``True``.

    A JSON body carries a real boolean; multipart carries only text, and the
    capitalised Python spelling is not what any vendor parses.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_multipart(plan: RequestPlan, payload: Any) -> aiohttp.FormData:
    """Structured fields plus binary parts, as multipart/form-data.

    Text parts come from ``plan.form`` first and the request body second, so a
    script can supply them either way; binary parts come from ``plan.files``.
    The boundary and the Content-Type belong to aiohttp -- a caller-supplied
    Content-Type would omit the boundary and make the body unparseable, which
    is why ``build_request`` drops one before it gets here.
    """
    form = aiohttp.FormData()
    fields: dict[str, Any] = dict(plan.form or {})
    if isinstance(payload, dict):
        fields.update(payload)
    for name, value in fields.items():
        if value is not None:
            form.add_field(str(name), _as_text(value))
    for name, parts in (plan.files or {}).items():
        for filename, data, mime in parts:
            form.add_field(name, data, filename=filename, content_type=mime)
    return form


def _drop_content_type(headers: dict[str, str]) -> None:
    """Removes any Content-Type so aiohttp's multipart one (with its boundary)
    is the only one. Matching is case-insensitive: header names are."""
    for key in [k for k in headers if k.lower() == "content-type"]:
        del headers[key]


def build_request(
    channel: ChannelSpec,
    plan: RequestPlan,
    body: Any,
    default_url: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """Assembles (url, method, aiohttp kwargs) for one outbound call.

    Body precedence is raw > multipart > form > JSON: the more explicit the
    script was, the earlier it wins.
    """
    url = merge_url(default_url or channel.upstream_url, plan)
    method = plan.method or channel.method
    headers: dict[str, str] = dict(plan.headers)
    query: dict[str, str] = {}

    payload = plan.body if plan.body_set else body
    apply_auth(channel, headers, payload, query)
    if query:
        url = merge_url(url, RequestPlan(query=query))

    kwargs: dict[str, Any] = {"headers": headers}
    if plan.raw is not None:
        kwargs["data"] = plan.raw
    elif plan.files is not None:
        _drop_content_type(headers)
        kwargs["data"] = build_multipart(plan, payload)
    elif plan.form is not None:
        kwargs["data"] = plan.form
    elif method in {"GET", "DELETE"}:
        if isinstance(payload, dict) and payload:
            url = merge_url(
                url, RequestPlan(query={k: str(v) for k, v in payload.items()})
            )
    elif payload is not None:
        kwargs["json"] = payload
        headers.setdefault("Content-Type", JSON_CONTENT)

    return url, method, kwargs
