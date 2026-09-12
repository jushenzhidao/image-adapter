"""Data-plane settings.

The adapter owns no channel/model/billing knowledge: that is the control
plane's job (New API). Everything here is execution-engine policy only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import model_validator
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
    # Six bounds apply to one request and they are kept separate on purpose:
    # which one fired is what the caller sees, so merging them would make the
    # error code lie (AC-27).
    #
    #   script_timeout         each transform() call       504 script_timeout
    #   upstream_timeout       one upstream HTTP call      504 upstream_timeout
    #   poll_timeout_default   one async job, all polls    504 poll_timeout
    #   stage_budget_default   the whole cascade           504 pipeline_timeout
    #   image_download_timeout one client image fetch      504 image_download_timeout
    #   storage_upload_timeout one object-store upload     degrades (see below)
    #
    # Only upstream_timeout bounds waiting on the vendor, so that is the knob
    # for a slow model. script_timeout bounds the script function itself and
    # does NOT include that wait, so a slow generation never needs it raised.
    #
    # The last two are a different kind of bound: not a phase and not a
    # generation, but one action a script performs *inside* a phase. They exist
    # because script_timeout was carrying that job by accident -- an
    # unreachable client image ate the entire phase budget and then reported
    # `script_timeout`, which names the wrong layer and sends the reader to the
    # vendor. They are short for the same reason: fetching a reference image is
    # not a generation.
    #
    # They bound the action, never the phase. A phase that performs several
    # actions can still reach script_timeout, and that stays the honest answer
    # for "the script itself ran too long".

    # Wall clock for one transform() call, enforced by _call_phase.
    script_timeout: float = 30.0
    # Wall clock for one image fetch (ctx.download_image). Its job is
    # *attribution*, not policing: what stops a request running away is the
    # phase cap (script_timeout), and this bound only has to fire *first* so the
    # failure is reported as a download rather than as the script running long.
    #
    # That is why it sits just under script_timeout instead of far below it. Too
    # short and it fails work the phase would have finished: a 20 MB image -- the
    # hard per-image cap -- needs 20s at 1 MB/s, so a 15s bound turns legitimate
    # slow fetches into 504s. Too long (>= script_timeout) and it can never fire
    # first, so the misattribution this exists to fix comes back silently. Keep
    # the two related when either moves; tests/unit/test_config_keys.py asserts
    # the ordering.
    image_download_timeout: float = 25.0
    # Wall clock for one object-store upload (ctx.upload_temp_image). Same
    # attribution job as the download bound, and the same reason for sitting
    # just under script_timeout. It clears the fal backend's measured 21.97s
    # outlier against a ~1.6s median, so it only fires on an upload that is
    # genuinely wedged. A timeout here degrades to a data URI exactly like every
    # other storage failure -- the store is an accelerator -- and the trace
    # carries `storage_upload_timeout` as the reason.
    storage_upload_timeout: float = 25.0
    # Default wall clock for one outbound HTTP call, and the value that
    # decides how long a generation may take. The shipped .env raises it to
    # 180s because a 2K Ark generation measures 27~82s. A script may override
    # it per call via ctx.emit(timeout=...), and a cascade further clamps it
    # to whatever is left of the budget.
    upstream_timeout: float = 60.0
    # Reaching a script over the network (X-Script-Ref) is a fetch, not a
    # generation, so it gets its own short bound.
    remote_script_timeout: float = 10.0
    # Cadence and ceiling for async (job-based) upstreams. The ceiling is
    # separate from upstream_timeout because each poll is a fast request and
    # it is their sum that can run long.
    poll_interval_default: float = 2.0
    poll_timeout_default: float = 300.0
    #: One poll is a status check, not a generation, so it gets its own short
    #: bound: `poll_timeout_default` is only checked at the top of the loop, and
    #: a single poll allowed to run for `upstream_timeout` could therefore
    #: overrun the whole job budget by itself. Same reasoning as
    #: `remote_script_timeout`. A script can still ask for more per call with
    #: `ctx.emit(timeout=...)`, which wins over this.
    poll_request_timeout: float = 10.0
    poll_max_attempts: int = 600

    # --- Multi-stage pipelines ---------------------------------------------
    # A cascade makes several upstream calls, so per-stage timeouts alone
    # would sum well past what a client waits for. Stages share one budget
    # and each call is clamped to whatever is left of it.
    #
    # There is deliberately no per-stage timeout setting: a stage's upstream
    # call is bounded by upstream_timeout, or by the script's ctx.emit
    # override. The `stage_timeout` error code (adapter/errors.py) is not
    # tied to a setting -- it only reports that a *named* stage's call timed
    # out, as opposed to an unnamed single-call request.
    stage_budget_default: float = 300.0
    # Ceiling for a caller-supplied X-Stage-Timeout. The header is
    # attacker-reachable, so an unbounded budget would pin a worker.
    stage_budget_max: float = 600.0
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

    # --- Concurrent materialisation ----------------------------------------
    # How many per-item calls one ctx fan-out may have in flight. This is a
    # ceiling on *our* fan-out, not a throttle on the vendor: the pool above is
    # a per-worker resource limit, so exceeding it queues inside aiohttp and the
    # queued time is charged to the action's own timeout (adapter/utils/fanout).
    #
    # Five, and the bound is worth stating correctly because the note this
    # replaces got it wrong: it is *not* the request-phase budget
    # (script_timeout) that caps a useful value. Measured 2026-09-13 against a
    # latency-limited source, N=9 went from 927 ms at 3 to 368 ms at 9 -- 2.5x
    # -- with the phase budget nowhere near being reached. What caps it is
    # upstream behaviour, memory, and the pool above: peak memory is
    # fanout_concurrency x max_asset_bytes, and a degree past
    # HTTP_POOL_LIMIT_PER_HOST queues, with that wait charged to the action's
    # own timeout.
    #
    # At or below the degree the value is free rather than merely cheap: the
    # width actually used is min(this, N), so one reference never constructs the
    # semaphore at all and a text-to-image request never reaches the fan-out.
    # Five buys full overlap on the common two-to-four-reference edit and on a
    # five-reference one, and batches a ten-reference request (google's own
    # max_input_images ceiling) into two waves. See docs/07 §14.2.1.
    fanout_concurrency: int = 5

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
    # returns a presigned URL that expires, minio_public is the same bucket
    # read anonymously and returns a plain URL that does not, fal is a
    # two-step HTTP upload that returns a public URL that does not either.
    # Code that needs the difference reads StoredObject.visibility, never
    # this field.
    #
    # A Literal so a typo fails at startup. `extra="ignore"` would otherwise
    # turn "STORAGE_BACKND=fal" into a silent fallback to minio.
    #
    # minio_colocated is the two-address shape of one bucket: uploads prefer
    # MINIO_ENDPOINT (the internal address) and fall back to
    # MINIO_FALLBACK_ENDPOINT (the domain), while the URL handed back is always
    # the domain -- see storage/minio_colocated_store.py for why the two halves
    # are not interchangeable.
    storage_backend: Literal["minio", "minio_public", "minio_colocated", "fal"] = "minio"

    # Optional second backend, tried when the first cannot take an upload.
    # Empty keeps the previous behaviour: a failing store degrades to a data
    # URI and the caller still gets an answer.
    #
    # Two things to know before turning it on. The minio flavours hand out
    # presigned URLs and fal hands out public ones, so failing over changes
    # *what* the caller receives, not only whether it receives anything --
    # StoredObject.visibility says which one served. And the fallback has to be
    # configured in its own right (FAL_KEY for fal): an unconfigured fallback is
    # reported and skipped, never silently replaced by the primary.
    storage_fallback_backend: Literal[
        "", "minio", "minio_public", "minio_colocated", "fal"
    ] = ""
    # How long the primary is skipped after a failure, in seconds. A refused
    # connection costs ~6s per attempt (five urllib3 retries at the pool
    # settings the minio builder installs), so without this park every upload
    # would pay that for as long as the primary is down.
    storage_failover_cooldown: int = 30

    # Optional segment of the keys the engine builds, *inside* the day:
    # ``<yyyymmdd>/[<prefix>/]<uuid>.<ext>``. Empty is the default and puts the
    # date at the bucket root, which is how the buckets these channels write to
    # are already laid out. The date always leads -- a day-at-a-time bucket
    # lifecycle rule matches on that -- so a prefix sits below it, never in
    # front of it. Slashes are stripped, so "" and "/" mean the same thing and
    # neither can produce "<date>//...". Only the minio flavours see it -- fal
    # has no directories and flattens the key.
    storage_key_prefix: str = ""

    @model_validator(mode="after")
    def _fallback_must_differ_from_the_primary(self) -> Self:
        """A self-fallback is a config mistake, not a resilience setting.

        Caught here because the alternative is a store that tries the same
        backend twice, which looks like failover in the config and in logs
        while buying nothing.
        """
        if (
            self.storage_fallback_backend
            and self.storage_fallback_backend == self.storage_backend
        ):
            raise ValueError(
                "STORAGE_FALLBACK_BACKEND must differ from STORAGE_BACKEND "
                f"(both are {self.storage_backend!r})"
            )
        return self

    # --- Infra (empty = graceful degradation) ------------------------------
    redis_url: str = ""
    minio_endpoint: str = ""
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "adapter-temp"
    minio_secure: bool = False
    # The *in-cluster* address of the SAME bucket, used only by
    # STORAGE_BACKEND=minio_colocated: uploads try this one first (on the same node it is a
    # local hop) and fall back to MINIO_ENDPOINT. Empty means "not configured", which the
    # factory reports rather than working around -- a pod that quietly lost its internal
    # address looks exactly like one that never needed it.
    #
    # MINIO_ENDPOINT keeps meaning what it means for every other backend: the bucket's
    # reachable address, and the origin the URL is derived from. So switching backend is
    # "add this one key", not "repoint MINIO_ENDPOINT" -- and MINIO_SECURE (which applies
    # to MINIO_ENDPOINT) keeps meaning what it meant.
    #
    # A scheme may be carried in the value (http://minio:19000); without one it is
    # plaintext, the usual case for an in-cluster hop. Use https:// to say otherwise.
    minio_internal_endpoint: str = ""
    # Connect timeout for that address, in seconds. Deliberately short: it is also the
    # give-up cost that must precede the fallback, and an in-cluster address either answers
    # in milliseconds or is not reachable at all. The public address keeps the ordinary 300s.
    minio_internal_connect_timeout: float = 1.0
    # Per-host HTTP connection pool for object storage. minio-py's own default
    # is 10, which sits below the concurrency this service reaches; size it to
    # roughly the uploads expected in flight per worker. Only consulted when
    # MINIO_ENDPOINT is set, since storage is otherwise absent entirely.
    minio_pool_size: int = 20
    # Optional origin for the plain URLs STORAGE_BACKEND=minio_public hands
    # out. Empty means "derive it from MINIO_ENDPOINT and MINIO_BUCKET"
    # (path style), which is what a bare MinIO answers. Set it when the bucket
    # is fronted by something else -- a CDN, a custom domain, a gateway that
    # only routes a sub-path -- since the derived form would then name a host
    # the client cannot reach. A trailing slash is tolerated.
    minio_public_base_url: str = ""
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
    # Request headers are never captured, and turning this on is refused rather
    # than honoured: the channel contract puts X-Adapter-Key (data-plane
    # credential), Authorization (vendor credential), X-Auth-Emit (which names
    # the credential header per channel, e.g. x-goog-api-key) and X-Script
    # (executable source) in headers, so no fixed redaction list can cover them.
    # init_logfire raises rather than exporting them.
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
