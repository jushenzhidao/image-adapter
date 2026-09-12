"""X-Script / X-Script-64 *policy* as it reaches an HTTP caller.

The transport is pinned in test_script_transport.py and the escape grammar in
unit/test_channel_escapes.py. Neither covers the guards that decide whether an
inline source is allowed to run at all -- the size cap, the deployment switch,
the digest pin, the allowlist, and the sandbox's refusals. Those all live in
adapter/script_source.py and were exercised only at unit level, where the
error envelope and the guards' ordering are invisible.

One deliberate emphasis: sizes here are the *unescaped* source. That is what
_enforce_size measures, and it is not obvious -- the header carrying a script
is always larger than the script itself, and X-Script-64 is half again as
large. A test that sent a base64 payload bigger than the cap and expected a
rejection would be asserting the opposite of the contract.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.testclient import TestClient

from adapter.settings import Settings

ADAPTER_KEY = "test-adapter-key"

#: The code default for Settings.max_inline_script_bytes. Restated rather than
#: imported: if the default moves, this suite should fail loudly instead of
#: quietly testing the new number on both sides.
SCRIPT_LIMIT = 8192

UPSTREAM_IMAGE = "https://cdn.vendor.test/t.png"


class _Vendor(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    server = HTTPServer(("127.0.0.1", 0), _Vendor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1/gen"
    server.shutdown()
    server.server_close()


_DEFAULTS = dict(
    environment="dev",
    adapter_key=ADAPTER_KEY,
    adapter_key_required=True,
    allow_inline_script=True,
    upstream_allow_private_network=True,
    redis_url="",
    storage_backend="minio",
    minio_endpoint="",
    fal_key="",
)


@contextlib.contextmanager
def _client(**overrides):
    """A real lifespan with inline scripts allowed, unless a test says not."""
    from adapter.main import app

    settings = Settings(**{**_DEFAULTS, **overrides})
    app.state.settings = settings
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.state.settings = None


def _source() -> str:
    """A transform that produces a well-formed images response."""
    return (
        "async def transform(ctx, payload, phase):\n"
        "    if phase == 'request':\n"
        "        return {'prompt': payload['prompt']}\n"
        "    return {'data': [{'url': '%s'}]}\n" % UPSTREAM_IMAGE
    )


_HEAD = "async def transform(ctx, payload, phase):\n    # "
_TAIL = (
    "\n    if phase == 'request':\n"
    "        return {'prompt': payload['prompt']}\n"
    "    return {'data': [{'url': '%s'}]}\n" % UPSTREAM_IMAGE
)


def _source_of_size(size: int) -> str:
    """The same transform padded with an ASCII comment to an exact byte count.

    ASCII padding keeps bytes == characters, so the assertion is meaningful
    on its own terms rather than only in the ASCII case.
    """
    fill = size - len(_HEAD) - len(_TAIL)
    assert fill >= 0, "%d bytes cannot hold the skeleton" % size
    text = _HEAD + "x" * fill + _TAIL
    assert len(text.encode("utf-8")) == size
    return text


def _post(client, vendor: str, source: str | None = None, b64: str | None = None, **extra):
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": vendor,
        "Content-Type": "application/json",
    }
    if source is not None:
        headers["X-Script"] = source.replace("\n", "\\n")
    if b64 is not None:
        headers["X-Script-64"] = b64
    headers.update(extra)
    return client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )


def _code(resp) -> str | None:
    return resp.json()["error"]["code"]


def test_a_working_inline_script_reaches_the_upstream(vendor):
    """Baseline: the whole path -- parse, unescape, verify, compile, run."""
    with _client() as client:
        resp = _post(client, vendor, _source())
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == UPSTREAM_IMAGE
    assert resp.headers["X-Script-Sha256"]


def test_source_exactly_at_the_limit_is_accepted(vendor):
    with _client() as client:
        resp = _post(client, vendor, _source_of_size(SCRIPT_LIMIT))
    assert resp.status_code == 200, resp.text


def test_source_one_byte_over_the_limit_is_refused(vendor):
    with _client() as client:
        resp = _post(client, vendor, _source_of_size(SCRIPT_LIMIT + 1))
    assert resp.status_code == 400
    assert _code(resp) == "script_too_large"


def test_the_cap_measures_the_source_not_the_header(vendor):
    """X-Script-64 removes escaping, not volume.

    A source that fits is accepted even though its base64 is a third longer
    than the cap, because _enforce_size reads the decoded text. Pinned because
    the opposite is the intuitive guess.
    """
    source = _source_of_size(SCRIPT_LIMIT)
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    assert len(encoded) > SCRIPT_LIMIT, "the encoded form must exceed the cap"

    with _client() as client:
        resp = _post(client, vendor, b64=encoded)
    assert resp.status_code == 200, resp.text


def test_inline_is_refused_when_the_deployment_disables_it(vendor):
    with _client(allow_inline_script=False) as client:
        resp = _post(client, vendor, _source())
    assert resp.status_code == 403
    assert _code(resp) == "script_forbidden"


def test_disabling_inline_also_disables_the_base64_form(vendor):
    """One switch, two headers: there is no 'keep X-Script-64 only' setting."""
    encoded = base64.b64encode(_source().encode("utf-8")).decode("ascii")
    with _client(allow_inline_script=False) as client:
        resp = _post(client, vendor, b64=encoded)
    assert resp.status_code == 403
    assert _code(resp) == "script_forbidden"


def test_sha256_pin_admits_the_matching_digest(vendor):
    source = _source()
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with _client() as client:
        resp = _post(client, vendor, source, **{"X-Script-Sha256": digest})
    assert resp.status_code == 200, resp.text


def test_sha256_pin_refuses_a_mismatch(vendor):
    with _client() as client:
        resp = _post(client, vendor, _source(), **{"X-Script-Sha256": "0" * 64})
    assert resp.status_code == 400
    assert _code(resp) == "script_integrity_error"


def test_allowlist_admits_the_listed_digest(vendor):
    source = _source()
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with _client(script_sha256_allowlist=digest) as client:
        resp = _post(client, vendor, source)
    assert resp.status_code == 200, resp.text


def test_allowlist_refuses_an_unlisted_script(vendor):
    with _client(script_sha256_allowlist="a" * 64) as client:
        resp = _post(client, vendor, _source())
    assert resp.status_code == 403
    assert _code(resp) == "script_forbidden"


def test_invalid_base64_is_refused(vendor):
    with _client() as client:
        resp = _post(client, vendor, b64="not base64 at all !!")
    assert resp.status_code == 400
    assert "base64" in resp.json()["error"]["message"]


def test_base64_that_does_not_decode_to_utf8_is_refused(vendor):
    encoded = base64.b64encode(b"\xff\xfe\x00").decode("ascii")
    with _client() as client:
        resp = _post(client, vendor, b64=encoded)
    assert resp.status_code == 400
    assert "UTF-8" in resp.json()["error"]["message"]


def test_two_source_headers_are_refused(vendor):
    """Exactly one of the three is the contract; two is ambiguous, not merged."""
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": vendor,
        "Content-Type": "application/json",
        "X-Script": _source().replace("\n", "\\n"),
        "X-Script-64": base64.b64encode(_source().encode()).decode(),
    }
    with _client() as client:
        resp = client.post(
            "/v1/images/generations", headers=headers, json={"prompt": "cat"}
        )
    assert resp.status_code == 400
    assert _code(resp) == "channel_config_error"


def test_whitespace_only_source_counts_as_absent(vendor):
    """An empty-looking header is treated as not sent, so the error names the
    missing header rather than blaming the script's content."""
    with _client() as client:
        resp = _post(client, vendor, "   ")
    assert resp.status_code == 400
    assert _code(resp) == "channel_config_error"
    assert "is required" in resp.json()["error"]["message"]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    import os\n    return payload\n", id="import"),
        pytest.param(
            "    return {'x': open('/etc/passwd').read()}\n", id="open"
        ),
        pytest.param("    return {'x': ().__class__}\n", id="dunder"),
        pytest.param("    exec('pass')\n    return payload\n", id="exec"),
        pytest.param("    return {'x': globals()}\n", id="globals"),
    ],
)
def test_the_sandbox_refuses_forbidden_constructs(vendor, body):
    source = "async def transform(ctx, payload, phase):\n" + body
    with _client() as client:
        resp = _post(client, vendor, source)
    assert resp.status_code == 400, resp.text
    assert _code(resp) == "script_security_error"


def test_a_script_without_transform_is_refused(vendor):
    source = "async def other(ctx, payload, phase):\n    return payload\n"
    with _client() as client:
        resp = _post(client, vendor, source)
    assert resp.status_code == 400
    assert _code(resp) == "script_security_error"
    assert "transform" in resp.json()["error"]["message"]


def test_the_response_carries_the_digest_of_what_actually_ran(vendor):
    """Two different sources must not share a fingerprint -- the header is the
    audit handle a control plane compares against."""
    with _client() as client:
        first = _post(client, vendor, _source())
        second = _post(client, vendor, _source() + "\n# tweaked\n")
    assert first.status_code == 200 and second.status_code == 200
    assert first.headers["X-Script-Sha256"] != second.headers["X-Script-Sha256"]


# --- The README Vision sample, run for real ------------------------------
#
# The documented sample is the one script a reader is most likely to paste
# into a channel, so it is worth exercising as written rather than trusting
# that it still works. It leans on two ctx capabilities at once: image_b64
# (which downloads only for an http(s) reference) and upload_temp_image
# (which has a degraded branch when no object store is configured).

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)

README_VISION_SCRIPT = """async def transform(ctx, payload, phase):
    if phase == 'request':
        return {
            'desc': payload['prompt'],
            'image_b64': await ctx.image_b64(payload['image_url']),
        }

    url = await ctx.upload_temp_image(payload)
    return {'data': [{'url': url}]}
"""


class _Binary(BaseHTTPRequestHandler):
    """A vendor that answers with image bytes, as a real generator would."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1PX)))
        self.end_headers()
        self.wfile.write(PNG_1PX)

    def log_message(self, *args):
        pass


class _ImageSource(BaseHTTPRequestHandler):
    """Somewhere the reference image can actually be fetched from."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1PX)))
        self.end_headers()
        self.wfile.write(PNG_1PX)

    def log_message(self, *args):
        pass


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def binary_vendor():
    server = _serve(_Binary)
    yield f"http://127.0.0.1:{server.server_port}/v1/gen"
    server.shutdown()
    server.server_close()


@pytest.fixture
def image_source():
    server = _serve(_ImageSource)
    yield f"http://127.0.0.1:{server.server_port}/ref.png"
    server.shutdown()
    server.server_close()


def _post_readme_sample(client, vendor_url: str, image_url: str):
    headers = {
        "X-Adapter-Key": ADAPTER_KEY,
        "X-Upstream-Url": vendor_url,
        "Content-Type": "application/json",
        "X-Script": README_VISION_SCRIPT.replace("\n", "\\n"),
    }
    return client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "cat", "image_url": image_url},
    )


def test_readme_vision_sample_degrades_to_a_data_uri_without_storage(
    client, binary_vendor, image_source
):
    """The default settings configure no object store. The documented sample
    must still answer -- with the image inlined instead of linked."""
    resp = _post_readme_sample(client, binary_vendor, image_source)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"].startswith("data:image/")


def test_readme_vision_sample_uploads_when_storage_is_configured(
    client, storage, binary_vendor, image_source
):
    """With a store present, the same script produces a real link -- the other
    side of the branch the degraded case above leaves untested."""
    resp = _post_readme_sample(client, binary_vendor, image_source)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"].startswith("https://cdn.test/")
    assert len(storage.puts) == 1


def test_a_script_that_lost_its_indentation_is_reported_as_a_syntax_error(vendor):
    """The copy-paste failure mode: the sample flattened to one level, which
    is what happens when it travels through a chat message. The caller gets a
    named syntax error rather than a silently different script."""
    flattened = (
        "async def transform(ctx, payload, phase):\n"
        "if phase == 'request':\n"
        "return {'desc': payload['prompt']}\n"
    )
    with _client() as client:
        resp = _post(client, vendor, flattened)
    assert resp.status_code == 400
    assert _code(resp) == "script_security_error"
    assert "syntax error" in resp.json()["error"]["message"].lower()
