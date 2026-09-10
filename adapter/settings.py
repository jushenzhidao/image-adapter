"""Data-plane settings.

The adapter owns no channel/model/billing knowledge: that is the control
plane's job (New API). Everything here is execution-engine policy only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8080

    # --- Admission control -------------------------------------------------
    # X-Adapter-Key gates the data plane. Injecting Python source through a
    # header is remote code execution by design, so this must be set in any
    # non-local deployment. Authorization is NOT used here: it is reserved
    # for passthrough to the upstream vendor.
    adapter_key: str = ""
    adapter_key_required: bool = True

    # --- Script source policy ---------------------------------------------
    # Inline source (X-Script / X-Script-64) is convenient for development but
    # lets any caller run arbitrary code in-process. Production deployments
    # should disable it and rely on refs or a sha256 allowlist.
    allow_inline_script: bool = True
    allow_remote_script: bool = False
    # Comma-separated hosts that X-Script-Ref URLs may be fetched from.
    remote_script_hosts: str = ""
    # Comma-separated sha256 hex digests. Non-empty means allowlist-only mode:
    # every script, whatever its source, must hash into this set.
    script_sha256_allowlist: str = ""
    # Directory backing named refs such as vendor_y/mj@v1.3. This is the
    # image-baked store: it always exists, so refs resolve with no host
    # dependency, and it is the lowest-precedence backend.
    script_ref_dir: str = str(BASE_DIR / "script_store")
    # Optional comma-separated read-only volume mounts, searched BEFORE
    # script_ref_dir. Lets a deployment override or add a script without
    # rebuilding the image; absent directories are skipped, not an error.
    script_overlay_dirs: str = ""
    # When a store root ships manifest.json, may it refuse a script whose
    # sha256 disagrees? Off by default: the manifest declares intent, and an
    # unsigned local file must not become a hard gate on a working deployment.
    script_pin_manifest_digests: bool = False

    # Practical ceiling for inline scripts. ASGI servers cap the whole header
    # block (h11 allows 16 KiB total), so anything larger must use a ref.
    max_inline_script_bytes: int = 8192
    max_script_bytes: int = 262144
    script_cache_size: int = 256

    # --- Upstream URL policy (X-Upstream-Url is caller-supplied) -----------
    # A full URL from a header is an SSRF vector. Loopback/link-local/private
    # ranges are refused unless explicitly enabled for local development.
    upstream_allow_private_network: bool = True
    # Comma-separated hosts; empty means any public host is allowed.
    upstream_host_allowlist: str = ""

    # --- Timeouts ----------------------------------------------------------
    script_timeout: float = 30.0
    upstream_timeout: float = 60.0
    remote_script_timeout: float = 10.0
    poll_interval_default: float = 2.0
    poll_timeout_default: float = 300.0
    poll_max_attempts: int = 600

    # --- Multi-stage pipelines ---------------------------------------------
    # A cascade makes several upstream calls, so per-stage timeouts alone
    # would sum well past what a client waits for. Stages share one budget
    # and each call is clamped to whatever is left of it.
    stage_budget_default: float = 300.0
    # Ceiling for a caller-supplied X-Stage-Timeout. The header is
    # attacker-reachable, so an unbounded budget would pin a worker.
    stage_budget_max: float = 600.0
    stage_timeout: float = 120.0
    # Upper bound on stages per request; a long STAGES list is a cheap way to
    # multiply one inbound request into many outbound calls.
    stage_max_count: int = 8

    # --- HTTP client -------------------------------------------------------
    # One shared session per process; a per-request session would discard the
    # connection pool and re-run TLS on every call.
    http_pool_limit: int = 100
    http_pool_limit_per_host: int = 20
    http_dns_cache_ttl: int = 300
    http_keepalive_timeout: float = 30.0

    # --- Image assets ------------------------------------------------------
    # Client-supplied images (URL or base64) are untrusted and unbounded, so
    # both the download and the decode are capped.
    max_asset_bytes: int = 20 * 1024 * 1024
    asset_cache_max_bytes: int = 64 * 1024 * 1024
    # Decoded-pixel ceiling for ctx.image. A 20 MB file is a small download but
    # can decode into gigabytes of raster (a decompression bomb), so the guard
    # has to be on pixel count, not byte count.
    max_image_pixels: int = 50_000_000

    # --- Infra (empty = graceful degradation) ------------------------------
    redis_url: str = ""
    minio_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "adapter-temp"
    minio_secure: bool = False

    # --- Middleware --------------------------------------------------------
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 600
    cors_allow_origins: str = "*"

    # --- Observability -----------------------------------------------------
    logfire_token: str = ""

    # --- TTLs (seconds) ----------------------------------------------------
    img_cache_ttl: int = 300
    temp_image_ttl: int = 3600
    # Lifetime of a /v1/responses turn in the state chain.
    resp_ctx_ttl: int = 3600

    @property
    def script_overlay_list(self) -> tuple[str, ...]:
        """Overlay roots in declared order; empty means image-store only."""
        return tuple(
            p.strip() for p in self.script_overlay_dirs.split(",") if p.strip()
        )

    @property
    def remote_host_set(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.remote_script_hosts.split(",") if h.strip()
        )

    @property
    def upstream_host_set(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.upstream_host_allowlist.split(",") if h.strip()
        )

    @property
    def sha256_allowlist(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower() for d in self.script_sha256_allowlist.split(",") if d.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
