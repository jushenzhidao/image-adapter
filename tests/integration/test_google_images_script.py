"""The google/images@v1 script, end to end against a stand-in Gemini.

Two things this suite exists to prove, because they are where the money and the
silent failures are:

  * a client URL is **forwarded**, not fetched -- the script must send
    fileData.fileUri and never touch the network itself;
  * an upstream HTTP 200 with no image in it is a **failure**, and the caller
    must see a 4xx with a reason instead of an empty result.

The vendor is a stdlib HTTP server so the assertions read the bytes that would
have left the process, and it records every GET so a download cannot hide.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_1X1).decode("ascii")
PNG_DATA_URI = "data:image/png;base64," + PNG_B64

MODEL_PATH = "/v1beta/models/gemini-2.5-flash-image:generateContent"

#: Swapped per test; the vendor echoes whatever is set here.
IMAGE_REPLY = {
    "candidates": [
        {
            "content": {"role": "model", "parts": [
                {"text": "here you go"},
                {"inlineData": {"mimeType": "image/png", "data": PNG_B64}},
            ]},
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 25,
        "candidatesTokenCount": 1120,
        "thoughtsTokenCount": 100,
        "totalTokenCount": 1245,
        "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1120}],
    },
}


class _Vendor(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict]] = []
    gets: ClassVar[int] = 0
    reply: ClassVar[dict] = IMAGE_REPLY
    status: ClassVar[int] = 200

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length)

    def do_GET(self):
        type(self).gets += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1X1)))
        self.end_headers()
        self.wfile.write(PNG_1X1)

    def do_POST(self):
        raw = self._body()
        type(self).calls.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw or b"{}"),
            }
        )
        payload = json.dumps(type(self).reply).encode()
        self.send_response(type(self).status)
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
    _Vendor.calls = []
    _Vendor.gets = 0
    _Vendor.reply = IMAGE_REPLY
    _Vendor.status = 200
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _headers(vendor: str, **extra: str) -> dict[str, str]:
    """The channel New API would build for a Gemini key."""
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": vendor + MODEL_PATH,
        "X-Script-Ref": "google/images@v1",
        "X-Auth-Emit": "header:x-goog-api-key",
        "Authorization": "Bearer test-gemini-key",
        "Content-Type": "application/json",
    }
    headers.update(extra)
    return headers


def _call() -> dict:
    assert _Vendor.calls, "the upstream was never called"
    return _Vendor.calls[-1]


def _parts(call: dict) -> list:
    return call["body"]["contents"][0]["parts"]


# --- request direction ------------------------------------------------------


def test_text_to_image_rewrites_the_model_into_the_path(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "a fox", "size": "1024x1024"},
    )
    assert resp.status_code == 200, resp.text

    call = _call()
    assert call["path"] == "/v1beta/models/gemini-3-pro-image:generateContent"
    # The credential goes exactly where X-Auth-Emit says, with no Bearer prefix.
    assert call["headers"]["x-goog-api-key"] == "test-gemini-key"
    assert "authorization" not in call["headers"]


def test_the_body_is_gemini_shaped_and_carries_no_openai_fields(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "a fox", "size": "1024x1024"},
    )
    assert resp.status_code == 200, resp.text

    body = _call()["body"]
    assert _parts(_call())[0] == {"text": "a fox"}
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert body["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }
    for absent in ("n", "size", "response_format", "model"):
        assert absent not in body, f"{absent} has no place in a generateContent body"


def test_a_client_url_is_forwarded_and_never_fetched(client, vendor):
    """The whole point of file_data: file_uri costs us nothing."""
    url = "https://cdn.example.com/photos/a.jpeg"
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "edit", "image": [url,
              "https://cdn.example.com/photos/b.png"]},
    )
    assert resp.status_code == 200, resp.text

    refs = _parts(_call())[1:]
    # camelCase is load-bearing: a live gateway dropped the snake_case spelling
    # and the upstream answered 500 (see the script's CONTRACT STATUS).
    assert refs[0] == {"fileData": {"mimeType": "image/jpeg", "fileUri": url}}
    # mime comes from the extension: b.png has no sniffing behind it either.
    assert refs[1]["fileData"]["mimeType"] == "image/png"
    assert _Vendor.gets == 0, "a forwarded URL must not be downloaded by us"


def test_a_small_client_image_is_inlined_with_its_mime(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "edit", "image": PNG_DATA_URI},
    )
    assert resp.status_code == 200, resp.text
    part = _parts(_call())[1]
    assert part["inlineData"] == {"mimeType": "image/png", "data": PNG_B64}


def test_a_bare_base64_image_is_decoded_only_to_learn_its_mime(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "edit", "image": PNG_B64},
    )
    assert resp.status_code == 200, resp.text
    assert _parts(_call())[1]["inlineData"]["mimeType"] == "image/png"


def test_no_part_uses_the_snake_case_spelling(client, vendor):
    """A live gateway dropped these as unknown fields and the upstream answered 500.

    Pinning the spelling here means a well-meaning "cleanup" back to snake_case
    fails in the suite instead of in production.
    """
    url = "https://cdn.example.com/photos/a.jpeg"
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "edit",
              "image": [PNG_DATA_URI, url]},
    )
    assert resp.status_code == 200, resp.text

    refs = _parts(_call())[1:]
    assert set(refs[0]) == {"inlineData"}
    assert set(refs[1]) == {"fileData"}
    flat = json.dumps(refs)
    for snake in ("inline_data", "file_data", "mime_type", "file_uri"):
        assert snake not in flat, f"{snake} must not be sent"


def test_the_client_response_format_beats_the_channel_default(client, vendor, storage):
    """Proves the request phase's value survived into the response phase.

    The response phase never sees the client body, so `response_format` has to
    travel through the script's own state. Running the two requests so that the
    asked-for value and the channel default disagree is what makes the override
    visible: if the default always won, the first request would have uploaded and
    returned a link. The fake store is what lets the default's branch be seen at
    all -- without storage both branches end in base64 and prove nothing.
    """
    options = json.dumps({"default_response_format": "url"})
    body = {"model": "nano-banana-pro", "prompt": "a fox"}

    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, **{"X-Channel-Options": options}),
        json=dict(body, response_format="b64_json"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["b64_json"] == PNG_B64
    assert storage.puts == [], "b64_json was asked for; nothing should be uploaded"

    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, **{"X-Channel-Options": options}),
        json=body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"].startswith("https://cdn.test/")
    assert len(storage.puts) == 1


# --- response direction -----------------------------------------------------


def test_inline_image_becomes_a_b64_response_with_mapped_usage(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "a fox",
              "response_format": "b64_json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"][0]["b64_json"] == PNG_B64
    # thoughts tokens are billed as output, so they must be inside output_tokens.
    assert body["usage"]["output_tokens"] == 1220
    assert body["usage"]["total_tokens"] == 1245
    assert body["usage"]["output_tokens_details"]["image_tokens"] == 1120
    assert body["gemini_usage"]["promptTokenCount"] == 25


@pytest.mark.parametrize(
    "reply, code",
    [
        ({"candidates": [{"finishReason": "IMAGE_SAFETY"}]}, "content_filter"),
        ({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}, "content_filter"),
        ({"candidates": [{"content": {"parts": [{"text": "I can't help."}]},
                          "finishReason": "STOP"}]}, "no_image_generated"),
        ({"candidates": [{"finishReason": "NO_IMAGE"}]}, "no_image_generated"),
    ],
)
def test_a_200_without_an_image_is_reported_as_a_failure(client, vendor, reply, code):
    """The upstream's most common failure mode arrives with HTTP 200."""
    _Vendor.reply = reply
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "something blocked"},
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == code
    assert error["param"] == "prompt"


def test_a_refusal_message_is_carried_back_to_the_caller(client, vendor):
    _Vendor.reply = {
        "candidates": [{"content": {"parts": [{"text": "I can't help with that."}]},
                        "finishReason": "STOP"}]
    }
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "x"},
    )
    assert resp.status_code == 400
    assert "I can't help with that." in resp.json()["error"]["message"]


def test_an_upstream_error_status_is_passed_through(client, vendor):
    _Vendor.status = 400
    _Vendor.reply = {"error": {"code": 400, "message": "API key not valid",
                               "status": "INVALID_ARGUMENT"}}
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert "API key not valid" in resp.json()["error"]["message"]


# --- requests this upstream cannot serve ------------------------------------


def test_n_greater_than_one_is_refused_before_the_call(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "x", "n": 3},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unsupported_parameter"
    assert not _Vendor.calls, "nothing should reach the upstream"


def test_a_mask_is_refused_rather_than_dropped(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "x", "image": PNG_DATA_URI,
              "mask": PNG_DATA_URI},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "mask"


def test_an_unknown_model_is_refused(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "gpt-image-1", "prompt": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["param"] == "model"


def test_url_response_format_without_storage_falls_back_to_base64(client, vendor):
    """No MinIO in the test settings, so there is no link to hand back.

    The answer is neither to fail the request over our own missing configuration
    nor to pass a data URI off as a link: it is the upstream's own shape, which
    for Gemini is base64. Same rule as openai/images@v1, so the two channels
    answer a caller identically here.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor),
        json={"model": "nano-banana-pro", "prompt": "x", "response_format": "url"},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["data"][0]
    assert item["b64_json"] == PNG_B64
    assert "url" not in item


def test_channel_options_can_pin_the_config_style_and_ratio(client, vendor):
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(
            vendor,
            **{
                "X-Channel-Options": json.dumps(
                    {"image_config_style": "responseFormat", "model": "nano-banana-2"}
                )
            },
        ),
        json={"prompt": "a fox", "size": "1920x1080"},
    )
    assert resp.status_code == 200, resp.text
    call = _call()
    assert call["path"] == "/v1beta/models/gemini-3.1-flash-image:generateContent"
    assert call["body"]["generationConfig"]["responseFormat"]["image"] == {
        "aspectRatio": "16:9",
        "imageSize": "2K",
    }
