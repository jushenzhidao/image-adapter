"""Outbound-call plumbing shared by the single-stage and cascade engines.

Split out of executor.py to keep each module within the per-file size budget.
Nothing here knows about phases or stages: it turns a RequestPlan plus a
ChannelSpec into one HTTP call and a normalised reply.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from adapter.channel import ChannelSpec
from adapter.ctxapi import RequestPlan
from adapter.errors import UpstreamError

JSON_CONTENT = "application/json"


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


def parse_body(raw: bytes, content_type: str) -> Any:
    """Decodes a JSON reply, or returns None when the body is not JSON."""
    looks_json = "json" in content_type or (
        raw[:1] in (b"{", b"[") if raw else False
    )
    if not looks_json:
        return None
    try:
        return _json.loads(raw.decode("utf-8"))
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


def build_request(
    channel: ChannelSpec,
    plan: RequestPlan,
    body: Any,
    default_url: str | None,
) -> tuple[str, str, dict[str, Any]]:
    """Assembles (url, method, aiohttp kwargs) for one outbound call."""
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
