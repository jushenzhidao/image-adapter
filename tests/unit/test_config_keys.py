"""Guards against dead configuration keys.

`Settings` sets `extra="ignore"`, so a key that no longer matches a field is
silently dropped instead of raising. That failure mode is invisible: the
deployment looks configured and behaves as if the line were absent. It has
already bitten this repo twice — `ADAPTER_AUTH_ENABLED` (the real fields are
`adapter_key` / `adapter_key_required`, so admission control stayed on and
every request 401'd) and `UPSTREAMS_CONFIG_PATH` (pointed at a registry that
was never implemented).

These tests diff every shipped config source against the real field set.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from adapter.settings import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

FIELDS = {name.upper() for name in Settings.model_fields}

ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=")

# Keys that are legitimately not Settings fields. Each needs a reason: this
# list is the only sanctioned way to keep a non-field key in a config file.
ALLOWED_NON_FIELDS = {
    # Consumed by a human running curl against the vendor directly; the
    # adapter never reads upstream credentials from the environment.
    "VOLCENGINE_ARK_API_KEY",
}

# Same idea for compose, which also configures the sidecar services.
ALLOWED_COMPOSE_NON_FIELDS = ALLOWED_NON_FIELDS | {
    "MINIO_ROOT_USER",  # minio server's own credentials, not the adapter's
    "MINIO_ROOT_PASSWORD",
}


def _env_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE.match(line)
        if match:
            keys.append(match.group(1))
    return keys


@pytest.mark.parametrize("filename", [".env.example"])
def test_env_file_has_no_dead_keys(filename: str) -> None:
    path = REPO_ROOT / filename
    if not path.exists():
        pytest.skip(f"{filename} is not present")
    dead = [k for k in _env_keys(path) if k not in FIELDS and k not in ALLOWED_NON_FIELDS]
    assert not dead, (
        f"{filename} sets keys that match no Settings field and would be "
        f"silently ignored: {dead}. Either rename them to the real field or "
        f"add them to ALLOWED_NON_FIELDS with a reason."
    )


@pytest.mark.parametrize("filename", [".env.example"])
def test_env_file_has_no_duplicate_keys(filename: str) -> None:
    path = REPO_ROOT / filename
    if not path.exists():
        pytest.skip(f"{filename} is not present")
    keys = _env_keys(path)
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"{filename} defines the same key twice: {dupes}"


def test_compose_adapter_service_has_no_dead_keys() -> None:
    path = REPO_ROOT / "docker-compose.yml"
    if not path.exists():
        pytest.skip("docker-compose.yml is not present")
    compose = yaml.safe_load(path.read_text(encoding="utf-8"))

    dead: dict[str, list[str]] = {}
    for service, spec in (compose.get("services") or {}).items():
        env = spec.get("environment") or []
        # Only the list form is used here; the mapping form would need
        # different parsing.
        if not isinstance(env, list):
            continue
        keys = [entry.split("=", 1)[0].strip() for entry in env if isinstance(entry, str)]
        bad = [
            k for k in keys if k not in FIELDS and k not in ALLOWED_COMPOSE_NON_FIELDS
        ]
        if bad:
            dead[service] = bad

    # Sidecars run other images and legitimately take their own variables.
    dead.pop("mock-upstream", None)
    dead.pop("redis", None)
    dead.pop("minio", None)

    assert not dead, (
        f"docker-compose.yml sets keys that match no Settings field and would "
        f"be silently ignored: {dead}"
    )


def test_dockerfile_copies_only_existing_paths() -> None:
    """A COPY of a missing directory fails the build, but only in CI.

    `COPY scripts` and `COPY config` shipped broken for a while because the
    image is never built locally (Docker Hub egress is unreliable here).
    """
    path = REPO_ROOT / "Dockerfile"
    if not path.exists():
        pytest.skip("Dockerfile is not present")

    missing = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()[1:]
        parts = [p for p in parts if not p.startswith("--")]
        if len(parts) < 2:
            continue
        for src in parts[:-1]:
            if not (REPO_ROOT / src).exists():
                missing.append(src)

    assert not missing, (
        f"Dockerfile copies paths that do not exist, so the build fails: {missing}"
    )
