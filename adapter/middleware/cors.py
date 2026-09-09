"""CORS middleware assembly. Uses Starlette's built-in CORSMiddleware with
origins from settings (comma-separated CORS_ALLOW_ORIGINS)."""

from __future__ import annotations

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from adapter.settings import Settings


def build_cors_middleware(settings: Settings) -> Middleware:
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    return Middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-Id"],
        max_age=86400,
    )
