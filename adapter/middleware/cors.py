"""CORS middleware assembly. Uses Starlette's built-in CORSMiddleware with
origins from settings (comma-separated CORS_ALLOW_ORIGINS)."""

from __future__ import annotations

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from adapter.settings import Settings

# This service is called by the control plane server-side, but the channel
# contract travels in headers, and a browser preflight rejects any header that
# is not listed here. Kept in step with adapter.main.channel_contract.
CHANNEL_HEADERS = [
    "X-Upstream-Url",
    "X-Upstream-Method",
    "X-Script",
    "X-Script-64",
    "X-Script-Ref",
    "X-Script-Sha256",
    "X-Adapter-Key",
    "X-Auth-Emit",
    "X-Async",
    "X-Channel-Options",
]

ALWAYS_ALLOWED_HEADERS = ["Content-Type", "Authorization", "X-Request-Id"]


def build_cors_middleware(settings: Settings) -> Middleware:
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    return Middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[*CHANNEL_HEADERS, *ALWAYS_ALLOWED_HEADERS],
        max_age=86400,
    )
