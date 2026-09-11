"""Data-plane settings.

The adapter owns no channel/model/billing knowledge: that is the control
plane's job (New API). Everything here is execution-engine policy only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    # Hard ceiling on a single upstream response body. Every reply is buffered
    # in full -- one buffer per in-flight request, see ``executor._do_upstream``
    # -- so this is the value that bounds worker memory: concurrency x this
    # number is the worst case. A layer-decomposition reply carries up to 11
    # images and measures 10~30 MB, so the default leaves headroom while still
    # refusing a body that could only be a mistake or an attack.
    max_upstream_bytes: int = 64 * 1024 * 1024

    # Hard ceiling on ONE inbound request body. Starlette's ``Request.body()``
    # and ``Request.form()`` both accumulate with no limit of their own -- the
    # multipart route accepts up to 64 files and reads each into memory -- so
    # without this a caller decides how much of a worker's memory to consume.
    # A reverse proxy refuses this earlier and for free (client_max_body_size),
    # and in a deployment that has one it should; this setting exists because
    # the adapter cannot assume a proxy is in front of it. Keep the two aligned.
    max_request_bytes: int = 64 * 1024 * 1024

    # --- Image assets ------------------------------------------------------
    # Client-supplied images (URL or base64) are untrusted and unbounded, so
    # both the download and the decode are capped.
    max_asset_bytes: int = 20 * 1024 * 1024
    asset_cache_max_bytes: int = 64 * 1024 * 1024
    # Decoded-pixel ceiling for ctx.image. A 20 MB file is a small download but
    # can decode into gigabytes of raster (a decompression bomb), so the guard
    # has to be on pixel count, not byte count.
    max_image_pixels: int = 50_000_000

    # --- Object storage ----------------------------------------------------
    # Which store the engine uploads to. A name, not an address, because the
    # backends differ in protocol rather than only in URL: minio is S3 and
    # returns a presigned URL that expires, fal is a two-step HTTP upload that
    # returns a public one that does not. Code that needs the difference reads
    # StoredObject.visibility, never this field.
    #
    # A Literal so a typo fails at startup. `extra="ignore"` would otherwise
    # turn "STORAGE_BACKND=fal" into a silent fallback to minio.
    storage_backend: Literal["minio", "fal"] = "minio"

    # --- Infra (empty = graceful degradation) ------------------------------
    redis_url: str = ""
    minio_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "adapter-temp"
    minio_secure: bool = False
    # Per-host HTTP connection pool for object storage. minio-py's own default
    # is 10, which sits below the concurrency this service reaches; size it to
    # roughly the uploads expected in flight per worker. Only consulted when
    # MINIO_ENDPOINT is set, since storage is otherwise absent entirely.
    minio_pool_size: int = 20
    # fal.ai CDN credential, used only when STORAGE_BACKEND=fal. Injected into
    # the SDK explicitly rather than left to fal-client's own environment
    # lookup, so an inherited FAL_KEY cannot silently become the credential
    # this process uploads with.
    fal_key: str = ""

    # --- Middleware --------------------------------------------------------
    rate_limit_enabled: bool = False
    rate_limit_per_minute: int = 600
    cors_allow_origins: str = "*"

    # --- Observability -----------------------------------------------------
    logfire_token: str = ""
    # Service identity reported to Logfire. An empty version means "read it
    # from the installed package metadata", so the number in the dashboard
    # cannot drift from pyproject.toml the way a hardcoded literal did.
    logfire_service_name: str = "openai-adapter"
    logfire_service_version: str = ""
    # Head sampling ratio for spans. 1.0 (default) traces every request; lower
    # it to bound export volume at high QPS. Metrics and logs are unaffected.
    logfire_sample_rate: float = 1.0
    # Human-readable span output on stderr. Helpful locally, noise in
    # production; the remote export behaves identically either way.
    logfire_console: bool = True
    # Paths excluded from tracing. Every orchestrator probes /health on a
    # timer, so tracing it buries the requests that matter.
    logfire_excluded_urls: str = "/health"
    # Request headers are never captured by default, and turning this on is a
    # credential leak: the channel contract puts X-Adapter-Key (data-plane
    # credential), Authorization (vendor credential) and X-Script (executable
    # source) in headers. Enabling it requires scrubbing rules in
    # adapter/logfire_setup.py first.
    logfire_capture_headers: bool = False

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
    def capability_roots(self) -> tuple[Path, ...]:
        """Capability-table roots, in the script store's own precedence order.

        Overlay first, image store last: a deployment hot-fixes a measured table
        from a mounted volume without rebuilding the image, exactly as it can for
        a script. Each root gets the ``capabilities/`` subdirectory so a table
        travels next to the scripts it describes.
        """
        roots = [Path(p) for p in self.script_overlay_list]
        roots.append(Path(self.script_ref_dir))
        return tuple(root / "capabilities" for root in roots)

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
