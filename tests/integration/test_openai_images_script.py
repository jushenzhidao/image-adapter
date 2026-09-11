"""The openai/images@v1 script, end to end.

The script solves two problems. Outbound, OpenAI exposes two endpoints split by
transport (JSON generations, multipart edits), and the adapter's canonical body
carries that split in a single field. Inbound, OpenAI-compatible gateways are
unequal about `response_format`: some honour it, some hand back a data URI under
`url`, some ignore it and return `b64_json`. These tests drive the *shipped*
script by ref and read both directions off a real local server, so what is
asserted is what OpenAI would actually receive and what the caller would
actually get back.

A stdlib HTTP server stands in for the vendor and parses multipart with the
email package, which keeps the assertions independent of whichever multipart
implementation the adapter happens to use.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import threading
from email.parser import BytesParser
from email.policy import default as EMAIL_POLICY
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")
PNG_DATA_URI = f"data:image/png;base64,{PNG_B64}"

GEN_PATH = "/v1/images/generations"
EDIT_PATH = "/v1/images/edits"

#: What the vendor answers with unless a test says otherwise.
DEFAULT_RESPONSE = {
    "created": 1712345678,
    "data": [{"b64_json": PNG_B64}],
    "usage": {"total_tokens": 7},
}


def _parse_multipart(content_type: str, body: bytes) -> dict:
    """multipart body -> {"fields": {name: text}, "files": {name: [part, ...]}}.

    Each file part is (filename, content-type, bytes), which is everything a
    test needs to prove an image survived the trip intact.
    """
    msg = BytesParser(policy=EMAIL_POLICY).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\n\r\n" + body
    )
    fields: dict[str, str] = {}
    files: dict[str, list[tuple[str, str, bytes]]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is None:
            fields[name] = payload.decode("utf-8")
        else:
            files.setdefault(name, []).append(
                (filename, part.get_content_type(), payload)
            )
    return {"fields": fields, "files": files}


class _Vendor(BaseHTTPRequestHandler):
    requests: list[dict] = []
    #: Counts asset fetches. A conversion that needs no download must not make
    #: one, and a counter proves that where reading the log would not.
    gets: int = 0
    response: dict = DEFAULT_RESPONSE

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length)

    def do_GET(self):
        # The asset a URL-shaped client image is downloaded from.
        _Vendor.gets += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1X1)))
        self.end_headers()
        self.wfile.write(PNG_1X1)

    def do_POST(self):
        raw = self._body()
        content_type = self.headers.get("Content-Type", "")
        record: dict = {"path": self.path, "content_type": content_type}
        if content_type.startswith("multipart/form-data"):
            record.update(_parse_multipart(content_type, raw))
        else:
            record["body"] = json.loads(raw or b"{}")
        _Vendor.requests.append(record)

        payload = json.dumps(_Vendor.response).encode()
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Vendor.requests = []
    _Vendor.gets = 0
    _Vendor.response = copy.deepcopy(DEFAULT_RESPONSE)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _headers(vendor: str, **extra: str) -> dict[str, str]:
    """The channel the control plane would build: URL names generation only."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": f"{vendor}{GEN_PATH}",
        "X-Script-Ref": "openai/images@v1",
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def _last() -> dict:
    assert _Vendor.requests, "the upstream was never called"
    return _Vendor.requests[-1]


# --- text-to-image keeps the JSON endpoint ---------------------------------


def test_text_to_image_uses_the_generations_endpoint(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "model": "gpt-image-1", "size": "1024x1024"},
    )
    assert resp.status_code == 200, resp.text

    call = _last()
    assert call["path"] == GEN_PATH
    assert call["content_type"].startswith("application/json")
    assert call["body"]["prompt"] == "a fox"
    assert call["body"]["model"] == "gpt-image-1"
    assert "image" not in call["body"]


def test_response_and_usage_pass_through(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"][0]["b64_json"] == PNG_B64
    assert body["created"] == 1712345678
    # Billing is computed from this block, so it must not be dropped.
    assert body["usage"] == {"total_tokens": 7}


# --- image-to-image becomes a multipart upload -----------------------------


def test_image_is_uploaded_to_the_edits_endpoint_as_a_file_part(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "make it red", "image": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text

    call = _last()
    assert call["path"] == EDIT_PATH
    assert call["content_type"].startswith("multipart/form-data")
    assert call["fields"]["prompt"] == "make it red"
    filename, content_type, payload = call["files"]["image"][0]
    assert payload == PNG_1X1
    assert content_type == "image/png"
    assert filename.endswith(".png")


def test_a_url_image_is_fetched_and_uploaded_as_bytes(client, vendor):
    """The upstream must receive bytes, never the URL the client sent."""
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "x", "image": f"{vendor}/assets/a.png"},
    )
    assert resp.status_code == 200, resp.text
    assert _last()["files"]["image"][0][2] == PNG_1X1


def test_several_images_repeat_the_bracketed_field(client, vendor):
    """OpenAI's own spelling for a multi-image edit."""
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "blend", "image": [PNG_DATA_URI, PNG_DATA_URI]},
    )
    assert resp.status_code == 200, resp.text
    parts = _last()["files"]["image[]"]
    assert len(parts) == 2
    assert all(payload == PNG_1X1 for _, _, payload in parts)


def test_mask_rides_as_its_own_file_part(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "inpaint", "image": PNG_DATA_URI, "mask": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text
    call = _last()
    assert call["files"]["mask"][0][2] == PNG_1X1
    assert "mask" not in call["fields"]


def test_scalar_fields_become_text_parts_with_json_style_booleans(client, vendor):
    """multipart carries text only: 2 must read "2" and false must read "false"."""
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={
            "prompt": "x",
            "image": PNG_DATA_URI,
            "n": 2,
            "size": "1024x1024",
            "background": "transparent",
            "moderation": False,
        },
    )
    assert resp.status_code == 200, resp.text
    fields = _last()["fields"]
    assert fields["n"] == "2"
    assert fields["size"] == "1024x1024"
    assert fields["background"] == "transparent"
    assert fields["moderation"] == "false"


# --- the same script behind the adapter's own multipart front door ---------


def test_a_multipart_client_request_becomes_a_multipart_upstream_call(
    client, vendor
):
    """Full chain: OpenAI-SDK spelling in, OpenAI-native spelling out."""
    headers = _headers(vendor)
    # httpx sets the multipart Content-Type (with its boundary) itself.
    headers.pop("Content-Type")
    resp = client.post(
        EDIT_PATH,
        headers=headers,
        files={"image": ("cat.png", io.BytesIO(PNG_1X1), "image/png")},
        data={"prompt": "make it red"},
    )
    assert resp.status_code == 200, resp.text
    call = _last()
    assert call["path"] == EDIT_PATH
    assert call["files"]["image"][0][2] == PNG_1X1
    assert call["fields"]["prompt"] == "make it red"


# --- endpoint derivation ---------------------------------------------------


def test_edits_url_option_overrides_the_derived_sibling(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(
            vendor,
            **{"X-Channel-Options": json.dumps({"edits_url": f"{vendor}/custom/edit"})},
        ),
        json={"prompt": "x", "image": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text
    assert _last()["path"] == "/custom/edit"


def test_a_query_on_the_channel_url_survives_the_swap(client, vendor):
    """Azure-style deployments carry an api-version query on both endpoints."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": f"{vendor}{GEN_PATH}?api-version=2024-02-01",
        "X-Script-Ref": "openai/images@v1",
        "Content-Type": "application/json",
    }
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "x", "image": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text
    assert _last()["path"] == f"{EDIT_PATH}?api-version=2024-02-01"


def test_a_non_image_path_falls_back_to_the_sibling_segment(client, vendor):
    """The swap is on the last path segment, whatever it is."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": f"{vendor}/v1/gen",
        "X-Script-Ref": "openai/images@v1",
        "Content-Type": "application/json",
    }
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"prompt": "x", "image": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text
    assert _last()["path"] == "/v1/edits"


# --- output shape: the caller's response_format is delivered, not forwarded --


def test_url_against_a_b64_upstream_without_storage_passes_through(client, vendor):
    """The upstream ignored response_format and there is nowhere to re-host it.

    The missing object storage is our configuration gap, so the upstream's own
    answer is passed through instead of failing the request. What must not
    happen is a `url` key invented around the base64.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "url"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["b64_json"] == PNG_B64
    assert "url" not in item
    assert _Vendor.gets == 0


def test_url_against_a_data_uri_upstream_without_storage_passes_through(
    client, vendor
):
    """The upstream's `url` was already a data URI; without storage it stands.

    Conversion would need object storage; the honest fallback is to return
    what the upstream said rather than to 502 or to dress it up.
    """
    _Vendor.response = {
        "created": 1712345678,
        "data": [{"url": PNG_DATA_URI, "width": 1}],
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "url"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["url"] == PNG_DATA_URI
    assert item["width"] == 1
    assert _Vendor.gets == 0


def test_a_data_uri_upstream_becomes_a_link_when_storage_is_configured(
    client, vendor, storage
):
    """The gateway shape the adapter exists for, with a working object store.

    Measured against subdirect.aicodexvip.top: it answers response_format=url
    with a data URI. With storage configured the caller gets the link it asked
    for, and the bytes come from decoding that data URI rather than a download.
    """
    _Vendor.response = {
        "created": 1712345678,
        "data": [{"url": PNG_DATA_URI, "width": 1}],
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "url"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["url"].startswith("https://cdn.test/")
    assert item["width"] == 1
    assert len(storage.puts) == 1
    key, content_type, raw = storage.puts[0]
    assert raw == PNG_1X1
    # <yyyymmdd>/<request-id>/<uuid>.<ext>: the day leads so a bucket lifecycle
    # rule can expire a day at a time, and there is no prefix unless a
    # deployment asks for one.
    day, request_id, name = key.split("/")
    assert len(day) == 8 and day.isdigit()
    assert request_id and name.endswith(".png")
    # The port carries the type through to the backend so the object is served
    # inline rather than as a download.
    assert content_type == "image/png"
    assert _Vendor.gets == 0


def test_an_item_with_no_payload_is_an_upstream_error(client, vendor):
    """Neither carrier present: nothing to convert and nothing to hand back."""
    _Vendor.response = {
        "created": 1712345678,
        "data": [{"revised_prompt": "a fox"}],
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "b64_json"},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"


def test_a_data_uri_under_url_becomes_b64_json_when_that_was_asked_for(
    client, vendor
):
    """The gateway's `url` was a data URI all along; b64_json is what was asked."""
    _Vendor.response = {
        "created": 1712345678,
        "data": [
            {
                "revised_prompt": "a fox",
                "url": PNG_DATA_URI,
                "width": 1,
                "height": 1,
            }
        ],
        "usage": {"total_tokens": 7},
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "b64_json"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["b64_json"] == PNG_B64
    assert "url" not in item
    # Vendor extras ride along: the response is a superset, like the request.
    assert item["revised_prompt"] == "a fox"
    assert item["width"] == 1 and item["height"] == 1
    # A data URI already is the image, so nothing should have been fetched.
    assert _Vendor.gets == 0


def test_a_link_from_the_upstream_is_fetched_for_b64_json(client, vendor):
    """b64_json against a link-answering upstream: download, then encode."""
    _Vendor.response = {
        "created": 1712345678,
        "data": [{"url": f"{vendor}/assets/a.png"}],
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "b64_json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["b64_json"] == PNG_B64
    assert _Vendor.gets == 1


def test_a_link_is_passed_through_untouched_for_url(client, vendor):
    """An upstream that honours response_format costs us no work at all."""
    link = f"{vendor}/assets/a.png"
    _Vendor.response = {"created": 1712345678, "data": [{"url": link}]}
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox", "response_format": "url"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == link
    assert _Vendor.gets == 0


def test_silence_means_pass_through(client, vendor):
    """No response_format: the upstream's own shape is the answer.

    The front door defaults the *field* to url for validation only. Reading
    that as an instruction here would send an un-asked-for image to object
    storage -- and 502 without it -- instead of riding through as it always
    has.
    """
    _Vendor.response = {"created": 1712345678, "data": [{"url": PNG_DATA_URI}]}
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == PNG_DATA_URI


def test_the_edits_front_door_normalises_the_response_too(client, vendor):
    """One pipeline, two transports: the response phase is not route-specific."""
    _Vendor.response = {"created": 1712345678, "data": [{"url": PNG_DATA_URI}]}
    headers = _headers(vendor)
    # httpx sets the multipart Content-Type (with its boundary) itself.
    headers.pop("Content-Type")
    resp = client.post(
        EDIT_PATH,
        headers=headers,
        files={"image": ("cat.png", io.BytesIO(PNG_1X1), "image/png")},
        data={"prompt": "make it red", "response_format": "b64_json"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["b64_json"] == PNG_B64
    assert _Vendor.gets == 0
