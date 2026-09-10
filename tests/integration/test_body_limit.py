"""Inbound request bodies are bounded before they are buffered.

Starlette's ``Request.body()`` and ``Request.form()`` both accumulate with no
ceiling of their own, and the multipart route reads up to 64 files into memory,
so without a guard the caller decides how much of a worker's memory to use.

The guard is one middleware rather than a check per handler, because the two
routes read the body differently. These tests cover both of them, and both ways
the size can become known:

  declared    the client states an oversized Content-Length, so the reply is
              refused before a byte of body is read;
  chunked     nothing is declared, so only the running total catches it -- and
              for multipart this one also proves the signal survives the
              handler's own broad ``except Exception`` around ``request.form()``;
  under       a body inside the cap still reaches the pipeline, so the guard
              cannot silently become a functional regression.
"""

from __future__ import annotations

import pytest

from adapter.settings import Settings

ADAPTER_KEY = "test-adapter-key"

# Small enough for a test to cross cheaply; the production default is 64 MB.
BODY_LIMIT = 4096

JSON_HEADERS = {"Content-Type": "application/json", "X-Adapter-Key": ADAPTER_KEY}
MULTIPART_HEADERS = {
    "Content-Type": "multipart/form-data; boundary=zz",
    "X-Adapter-Key": ADAPTER_KEY,
}


@pytest.fixture
def settings() -> Settings:
    """Overrides the shared fixture to shrink the ceiling to something reachable."""
    return Settings(
        environment="dev",
        adapter_key=ADAPTER_KEY,
        adapter_key_required=True,
        allow_inline_script=True,
        upstream_allow_private_network=True,
        redis_url="",
        minio_endpoint="",
        max_request_bytes=BODY_LIMIT,
    )


def _chunked_body():
    """A generator body: httpx sends it chunked, so no length is declared."""
    for _ in range(4):
        yield b"x" * BODY_LIMIT


def test_declared_oversize_is_refused_before_the_body_is_read(client):
    resp = client.post(
        "/v1/images/generations",
        content=b"x" * (BODY_LIMIT * 2),
        headers=JSON_HEADERS,
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "request_too_large"


def test_chunked_oversize_is_refused_while_the_body_arrives(client):
    resp = client.post(
        "/v1/images/generations", content=_chunked_body(), headers=JSON_HEADERS
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "request_too_large"


def test_multipart_route_is_covered_by_the_same_guard(client):
    """The two routes read the body differently (form() vs body()), which is
    exactly why the guard is one middleware instead of a check inside each
    handler -- a check on either path would miss the other.

    Chunked on purpose: this is the path where the parse failure happens inside
    ``normalise_edits_form``, whose ``except Exception`` around
    ``request.form()`` would turn any ordinary exception into a 400.
    """
    resp = client.post(
        "/v1/images/edits", content=_chunked_body(), headers=MULTIPART_HEADERS
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "request_too_large"


def test_body_within_the_cap_reaches_the_pipeline(client):
    """A body inside the ceiling passes the guard and lands on the channel
    contract check, which is the next thing that would reject it."""
    resp = client.post(
        "/v1/images/generations",
        content=b'{"prompt":"cat"}',
        headers=JSON_HEADERS,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "channel_config_error"
