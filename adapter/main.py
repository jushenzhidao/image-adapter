"""Starlette ASGI application: routes, middleware, lifecycle.

The app holds no channel or model state. Per-request adaptation state arrives
in headers; the only long-lived objects are the script cache and state store.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import aiohttp
import logfire
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route

from adapter.api.chat import chat_handler
from adapter.api.health import health_handler
from adapter.api.image_edits import image_edits_handler
from adapter.api.images import images_handler
from adapter.api.responses import responses_handler
from adapter.context import build_cache
from adapter.middleware.cors import build_cors_middleware
from adapter.middleware.logging import LoggingMiddleware
from adapter.middleware.rate_limit import RateLimitMiddleware
from adapter.script_cache import ScriptCache
from adapter.settings import get_settings
from adapter.state_store import StateStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: Starlette):
    # Tests may inject their own Settings onto app.state before startup;
    # only fall back to the environment-derived singleton when absent.
    cfg = getattr(app.state, "settings", None) or settings
    app.state.settings = cfg
    app.state.script_cache = ScriptCache(max_size=cfg.script_cache_size)
    app.state.state_store = StateStore(cfg)

    # One HTTP session for the whole process. Building it per request threw
    # away keep-alive and the DNS cache, which dominated latency on the
    # upstream call for every channel.
    app.state.http = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=cfg.upstream_timeout),
        connector=aiohttp.TCPConnector(
            limit=cfg.http_pool_limit,
            limit_per_host=cfg.http_pool_limit_per_host,
            ttl_dns_cache=cfg.http_dns_cache_ttl,
            keepalive_timeout=cfg.http_keepalive_timeout,
        ),
    )
    app.state.asset_cache = build_cache(cfg)

    if cfg.adapter_key_required and not cfg.adapter_key:
        logger.error(
            "ADAPTER_KEY is not set: every request will be rejected. "
            "Set ADAPTER_KEY, or ADAPTER_KEY_REQUIRED=false for local use only."
        )
    if cfg.allow_inline_script and cfg.environment != "dev":
        logger.warning(
            "Inline scripts are enabled outside dev. Header-supplied source is "
            "executed in-process; prefer X-Script-Ref with a sha256 allowlist."
        )

    logfire.info("adapter_startup", environment=cfg.environment)
    yield

    await app.state.http.close()
    close = getattr(app.state.asset_cache, "aclose", None)
    if close is not None:
        await close()
    await app.state.state_store.close()


logfire.configure(
    token=settings.logfire_token or None,
    service_name="openai-adapter",
    service_version="2.0.0",
    environment=settings.environment,
    send_to_logfire="if-token-present",
)

if not settings.logfire_token:
    logger.warning("Logfire running in local-only mode (LOGFIRE_TOKEN not set)")

routes = [
    Route("/health", health_handler, methods=["GET"]),
    Route("/v1/chat/completions", chat_handler, methods=["POST"]),
    Route("/v1/responses", responses_handler, methods=["POST"]),
    Route("/v1/images/generations", images_handler, methods=["POST"]),
    # Multipart front door for the route above; no separate script contract.
    Route("/v1/images/edits", image_edits_handler, methods=["POST"]),
]

middleware = [
    build_cors_middleware(settings),
    Middleware(LoggingMiddleware),
    Middleware(RateLimitMiddleware),
]

app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)

logfire.instrument_starlette(app)
