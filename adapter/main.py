"""FastAPI application: routes, middleware, lifecycle.

The app holds no channel or model state. Per-request adaptation state arrives
in headers; the only long-lived objects are the script cache, the state store,
one HTTP session and one object-storage client per process.

Two deliberate choices are worth knowing before editing this file:

* **No ``response_model`` anywhere.** Handlers return ``Response`` objects
  directly, which makes FastAPI skip validation and serialisation entirely.
  That is what lets a script-shaped payload (including the upstream's ``usage``
  block, which billing depends on) pass through untouched. Adding a
  ``response_model`` to a route that returns a ``dict`` would silently start
  trimming unknown fields.
* **Every channel header is declared optional.** ``channel_contract`` exists so
  the eleven headers appear in /docs and can be exercised from there, not to
  validate them. Required-header failures stay ``channel_config_error`` (400,
  OpenAI envelope) from ``adapter/channel.py`` instead of a FastAPI 422.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

import aiohttp
from fastapi import Depends, FastAPI, Header
from starlette.middleware import Middleware

from adapter.api.chat import chat_handler
from adapter.api.common import JSON_PARSER
from adapter.api.health import health_handler
from adapter.api.image_edits import image_edits_handler
from adapter.api.images import images_handler
from adapter.api.responses import responses_handler
from adapter.context import build_cache, build_storage
from adapter.error_handlers import install_error_handlers
from adapter.logfire_setup import init_logfire, resolve_service_version
from adapter.middleware.cors import build_cors_middleware
from adapter.middleware.logging import LoggingMiddleware
from adapter.middleware.rate_limit import RateLimitMiddleware
from adapter.script_cache import ScriptCache
from adapter.scriptstore import build_store
from adapter.settings import get_settings
from adapter.state_store import StateStore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

settings = get_settings()


async def channel_contract(
    x_upstream_url: Annotated[
        str | None,
        Header(alias="X-Upstream-Url", description="上游完整端点（必填）"),
    ] = None,
    x_script: Annotated[
        str | None,
        Header(alias="X-Script", description="内联脚本源码，换行写作字面量 \\n"),
    ] = None,
    x_script_64: Annotated[
        str | None,
        Header(alias="X-Script-64", description="脚本源码的 base64，避开 \\n 转义"),
    ] = None,
    x_script_ref: Annotated[
        str | None,
        Header(
            alias="X-Script-Ref",
            description="命名引用 vendor_y/mj@v1.3，或 https URL",
        ),
    ] = None,
    authorization: Annotated[
        str | None,
        Header(alias="Authorization", description="上游厂商凭证，原样透传，不用于本服务鉴权"),
    ] = None,
    x_adapter_key: Annotated[
        str | None,
        Header(alias="X-Adapter-Key", description="本服务准入密钥（必填）"),
    ] = None,
    x_upstream_method: Annotated[
        str | None, Header(alias="X-Upstream-Method", description="默认 POST")
    ] = None,
    x_auth_emit: Annotated[
        str | None,
        Header(
            alias="X-Auth-Emit",
            description="凭证位置非标准时使用，如 header:X-API-Key:Bearer",
        ),
    ] = None,
    x_async_: Annotated[
        str | None,
        Header(alias="X-Async", description="异步 Job 型上游，如 poll=2,timeout=300"),
    ] = None,
    x_script_sha256: Annotated[
        str | None, Header(alias="X-Script-Sha256", description="脚本完整性锁定")
    ] = None,
    x_channel_options: Annotated[
        str | None,
        Header(
            alias="X-Channel-Options",
            description="JSON 对象，脚本内经 ctx.options 读取",
        ),
    ] = None,
) -> None:
    """The channel contract expressed as code rather than prose.

    README documents these eleven headers in a table. Declaring them here means
    the contract cannot drift from the implementation, and /docs doubles as a
    test client for it: fill the fields in and send.

    Nothing is read or validated here -- ``adapter/channel.py`` parses the raw
    request, so failures keep their ``channel_config_error`` code and the
    OpenAI error envelope. That is also why every parameter is optional.
    """
    return None


# Attached to every OpenAI-compatible route so /docs lists the channel headers
# on each of them.
_CONTRACT = [Depends(channel_contract)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Tests may inject their own Settings onto app.state before startup;
    # only fall back to the environment-derived singleton when absent.
    cfg = getattr(app.state, "settings", None) or settings
    app.state.settings = cfg
    app.state.script_cache = ScriptCache(max_size=cfg.script_cache_size)
    # Ref-resolution chain: overlay volume mounts first, image-baked store
    # last. Built once, since a DirStore only resolves its root at startup.
    app.state.script_store = build_store(cfg)
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
    # Same reasoning as the HTTP session, one layer down: a per-request Minio
    # client discards the urllib3 pool, so every upload re-handshakes TLS.
    app.state.storage = build_storage(cfg)

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

    logger.info("adapter_startup json_parser=%s", JSON_PARSER)
    yield

    await app.state.http.close()
    close = getattr(app.state.asset_cache, "aclose", None)
    if close is not None:
        await close()
    await app.state.state_store.close()


middleware = [
    build_cors_middleware(settings),
    Middleware(LoggingMiddleware),
    Middleware(RateLimitMiddleware),
]

app = FastAPI(
    title="Image Adapter",
    summary="把任意厂商的图像/多模态 API 转成 OpenAI 标准端点",
    version=resolve_service_version(settings),
    lifespan=lifespan,
    middleware=middleware,
)

# Registered on the app rather than per handler, so the envelope also covers
# the exits a decorator cannot reach: router 404/405 and request validation.
install_error_handlers(app)

app.add_api_route("/health", health_handler, methods=["GET"], tags=["ops"])
app.add_api_route(
    "/v1/chat/completions",
    chat_handler,
    methods=["POST"],
    dependencies=_CONTRACT,
    tags=["openai"],
)
app.add_api_route(
    "/v1/responses",
    responses_handler,
    methods=["POST"],
    dependencies=_CONTRACT,
    tags=["openai"],
)
app.add_api_route(
    "/v1/images/generations",
    images_handler,
    methods=["POST"],
    dependencies=_CONTRACT,
    tags=["openai"],
)
# Multipart front door for the route above; no separate script contract.
app.add_api_route(
    "/v1/images/edits",
    image_edits_handler,
    methods=["POST"],
    dependencies=_CONTRACT,
    tags=["openai"],
)

init_logfire(app, settings)


__all__: list[Any] = ["app", "channel_contract", "lifespan"]
