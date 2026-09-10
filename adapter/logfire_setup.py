"""Logfire (OpenTelemetry) initialisation.

Three deliberate choices differ from the obvious implementation.

**Request headers are never captured.** The channel contract carries
``X-Adapter-Key`` (the data-plane credential), ``Authorization`` (the upstream
vendor's credential) and ``X-Script`` (executable Python source) as HTTP
headers, so capturing them would export credentials and source code to a third
party. ``capture_headers`` stays off, and scrubbing patterns back it up in case
a deployment turns the setting on.

**/health is excluded.** Every orchestrator probes it on a timer, and those
spans would outnumber real traffic by orders of magnitude and bury it.

**The reported version has one source.** It used to be a literal in two files
that had already drifted apart ("2.0.0" in main.py vs "1.0.0" here, against a
1.0.0 package). It now resolves settings -> installed metadata ->
pyproject.toml, so the dashboard cannot disagree with the artifact.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING

import logfire
from logfire import ConsoleOptions, SamplingOptions, ScrubbingOptions

from adapter.settings import BASE_DIR

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, WebSocket

    from adapter.settings import Settings

logger = logging.getLogger(__name__)

# Second line of defence behind capture_headers=False: anything matching one
# of these is replaced before a span leaves the process.
#
# No inline (?i) flags: logfire joins these with "|" into one pattern, and a
# global flag is only legal at the start of an expression. It compiles them
# with re.IGNORECASE anyway.
_SCRUB_PATTERNS: tuple[str, ...] = (
    r"x-adapter-key[\"']?\s*[:=]\s*\S+",
    r"authorization[\"']?\s*[:=]\s*\S+",
    r"x-script(?:-64)?[\"']?\s*[:=]\s*\S+",
)


def resolve_service_version(settings: Settings) -> str:
    """Single source of truth for the version reported to Logfire."""
    if settings.logfire_service_version:
        return settings.logfire_service_version
    try:
        return _pkg_version("image-adapter")
    except PackageNotFoundError:
        pass
    # The image copies the source tree without installing the package, so
    # distribution metadata is absent there; pyproject.toml is the next best
    # authority, and "unknown" is honest when neither is readable.
    try:
        import tomllib

        with open(BASE_DIR / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:  # noqa: BLE001 - any failure means "cannot determine"
        return "unknown"


def _request_attributes(request: Request | WebSocket, attributes: dict) -> dict:
    """Attach the adapter's request id so a trace can be found by it (BR-010)."""
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    if request_id:
        attributes["request_id"] = request_id
    return attributes


def init_logfire(app: FastAPI, settings: Settings) -> None:
    """Configure Logfire. Without a token it runs locally and exports nothing."""
    sample_rate = min(max(settings.logfire_sample_rate, 0.0), 1.0)
    service_version = resolve_service_version(settings)

    logfire.configure(
        token=settings.logfire_token or None,
        service_name=settings.logfire_service_name,
        service_version=service_version,
        environment=settings.environment,
        send_to_logfire="if-token-present",
        # Human-readable spans on stderr are useful locally and noise in
        # production; the remote export behaves identically either way.
        console=ConsoleOptions() if settings.logfire_console else False,
        sampling=SamplingOptions(head=sample_rate),
        scrubbing=ScrubbingOptions(extra_patterns=list(_SCRUB_PATTERNS)),
    )

    if settings.logfire_token:
        logger.info(
            "Logfire enabled: service=%s version=%s sampling=%.2f excluded=%s",
            settings.logfire_service_name,
            service_version,
            sample_rate,
            settings.logfire_excluded_urls or "(none)",
        )
    else:
        logger.warning("Logfire running in local-only mode (LOGFIRE_TOKEN not set)")

    logfire.instrument_fastapi(
        app,
        # See the module docstring: turning this on exports credentials and
        # executable source. The scrubbing patterns above are the backstop.
        capture_headers=settings.logfire_capture_headers,
        excluded_urls=settings.logfire_excluded_urls or None,
        request_attributes_mapper=_request_attributes,
    )
