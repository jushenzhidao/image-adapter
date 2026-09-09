"""Script transport forms: how source code survives the trip into a header.

Background: in a New API channel config the script sits in a JSON string,
where a line break is the two-character literal backslash-n. JSON transports
that natively (the codec handles escaping), which is why rules with newlines
"just work" over a JSON body. Headers are different: HTTP forbids a bare LF
in a header value, and the rejection happens in the HTTP/1.1 wire encoder
(h11) when the request is serialized onto the socket -- not when the client
object is constructed. These tests pin down all three journeys:

  1. The raw, undecoded JSON string value pasted straight into X-Script --
     already in the adapter's escaped form, works as-is.
  2. The decoded source (real newlines) sent via X-Script-64.
  3. A bare newline is refused by the wire encoder, while the escaped form
     passes it: the protocol is the constraint, not the adapter.

Note on test transport: Starlette's TestClient talks ASGI in-process and does
no wire encoding, so a bare LF would sail through it. Journey 3 therefore
asserts against h11 directly, which is what uvicorn and httpx actually use.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import h11
import pytest

# What you would see INSIDE a channel-config JSON file: one line, \n literals.
RAW_JSON_STRING_VALUE = (
    "async def transform(ctx, payload, phase):\\n"
    "    if phase == 'request':\\n"
    "        return {'desc': payload['prompt']}\\n"
    "    return {'data': [{'url': payload['image']}]}"
)

# The same script after json.loads: real newlines, ready for asteval-style use.
DECODED_SOURCE = json.loads(f'"{RAW_JSON_STRING_VALUE}"')


class _Vendor(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        payload = json.dumps({"image": "https://cdn.vendor.test/t.png"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    server = HTTPServer(("127.0.0.1", 0), _Vendor)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1/gen"
    server.shutdown()
    server.server_close()


def _base_headers(vendor_url: str) -> dict[str, str]:
    return {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": vendor_url,
        "Content-Type": "application/json",
    }


def test_raw_channel_json_value_works_in_x_script(client, vendor):
    """The undecoded JSON string form IS the header wire format. No extra
    escaping step: copy the value out of the channel config, put it in the
    header."""
    assert "\n" not in RAW_JSON_STRING_VALUE  # single line by construction

    headers = _base_headers(vendor)
    headers["X-Script"] = RAW_JSON_STRING_VALUE

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/t.png"


def test_decoded_source_works_via_x_script_64(client, vendor):
    """A control plane that already json-decoded the rule (real newlines)
    ships it base64ed. Same script, same digest space, same result."""
    assert "\n" in DECODED_SOURCE  # real newlines after decoding

    headers = _base_headers(vendor)
    headers["X-Script-64"] = base64.b64encode(
        DECODED_SOURCE.encode("utf-8")
    ).decode("ascii")

    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.vendor.test/t.png"


def test_both_forms_hit_the_same_cache_entry(client, vendor):
    """Unescaped X-Script and base64 X-Script-64 yield identical source text,
    so the response carries the same script fingerprint for both."""
    h1 = _base_headers(vendor)
    h1["X-Script"] = RAW_JSON_STRING_VALUE
    r1 = client.post("/v1/images/generations", headers=h1, json={"prompt": "a"})

    h2 = _base_headers(vendor)
    h2["X-Script-64"] = base64.b64encode(DECODED_SOURCE.encode()).decode()
    r2 = client.post("/v1/images/generations", headers=h2, json={"prompt": "a"})

    assert r1.status_code == r2.status_code == 200
    assert r1.headers["X-Script-Sha256"] == r2.headers["X-Script-Sha256"]


def _encode_on_the_wire(script: str) -> bytes:
    """Serialize a request through the real HTTP/1.1 encoder used by uvicorn
    and httpx. Raises LocalProtocolError on an illegal header value."""
    conn = h11.Connection(our_role=h11.CLIENT)
    return conn.send(
        h11.Request(
            method="POST",
            target="/v1/images/generations",
            headers=[("Host", "adapter.test"), ("X-Script", script)],
        )
    )


def test_bare_newline_is_refused_by_the_wire_encoder():
    """The constraint everyone trips on: a real LF in a header value cannot be
    serialized, so it never reaches the adapter."""
    with pytest.raises(h11.LocalProtocolError) as excinfo:
        _encode_on_the_wire(DECODED_SOURCE)
    assert "illegal header value" in str(excinfo.value).lower()


def test_escaped_form_survives_the_wire_encoder():
    """The flip side, and why the escaped form is the header contract: the
    same script goes out fine once the newlines are two-character literals."""
    wire = _encode_on_the_wire(RAW_JSON_STRING_VALUE)
    assert b"X-Script: async def transform" in wire
    assert b"\\n" in wire  # escaped, not a line break
