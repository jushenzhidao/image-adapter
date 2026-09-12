"""Unit tests do not read the developer's ``.env``.

``Settings`` loads ``.env`` by default, and that file is *production* configuration: a real
value in it silently changes what these tests assert. It has bitten twice on 2026-09-12,
both times while changing storage settings rather than test code:

  * ``MINIO_SECURE=true`` leaked into a URL assertion, which then failed on every machine
    whose ``.env`` said so (a long-standing "known fake red");
  * ``STORAGE_FALLBACK_BACKEND=fal`` wrapped every primary store in a ``FallbackStore``,
    failing 23 tests whose only sin was building settings without pinning that field.

The repo's own remedy so far was to pin the offending field in each helper
(``_settings()``, "isolated from .env for the fields that matter here"). That works, but it
makes every new storage setting a sweep across test files, and the failure mode when one is
missed is a red suite that looks like a regression in the code under test.

Nothing in this directory wants the deployment's file, so it is switched off once here.
Tests that want a particular value pass it explicitly -- ``Settings(minio_secure=True,
...)`` -- which is also what keeps the assertion readable. ``tests/integration`` is left
alone: those run the app, and some of them deliberately read the environment.
"""

from __future__ import annotations

import pytest

from adapter.settings import Settings


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stops ``Settings()`` from finding ``.env`` for the duration of each test.

    Patched on ``model_config`` rather than by unsetting variables: the values come from
    the *file*, so pydantic-settings reads it for every instantiation. ``model_config`` is
    a plain dict at runtime, and the settings source consults it on each ``Settings()``
    call, so this is the whole switch.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
