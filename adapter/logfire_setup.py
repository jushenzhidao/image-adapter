"""Logfire initialization for observability (AC-17). When LOGFIRE_TOKEN is not
set, degrades gracefully with send_to_logfire='if-token-present' (no error).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import logfire

if TYPE_CHECKING:
    from adapter.settings import Settings
    from starlette.applications import Starlette

logger = logging.getLogger(__name__)


def init_logfire(app: Starlette, settings: Settings) -> None:
    """Configure Logfire. When token is absent, runs locally without uploading."""
    logfire.configure(
        token=settings.logfire_token or None,
        service_name="openai-adapter",
        service_version="1.0.0",
        environment=settings.environment,
        send_to_logfire="if-token-present",
    )

    if settings.logfire_token:
        logger.info("Logfire enabled with remote export")
    else:
        logger.warning("Logfire running in local-only mode (LOGFIRE_TOKEN not set)")

    logfire.instrument_starlette(app)
