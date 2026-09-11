"""The four entries onto one canonical contract, end to end.

The regression these exist for: a chat body posted to an image channel used to
reach the script with no `prompt` at all, so the script sent an empty text part
and the vendor answered with a protobuf oneof complaint naming neither `prompt`
nor "empty". Every assertion below reads the bytes that would have left the
process, so a folding mistake cannot hide behind a tolerant stand-in.
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

GEMINI_PATH = "/v1beta/models/gemini-3.1-flash-image-preview:generateContent"

GEMINI_REPLY = {
    "candidates": [
        {
            "content": {
                "role": "model",
                "parts": [{"inlineData": {"mimeType": "image/png", "data": PNG_B64}}],
            },
            "finishReason": "STOP",
        }
    ],
    "usageMetadata": {
        "promptTokenCount": 11,
        "candidatesTokenCount": 1120,
        "totalTokenCount": 1131,
    },
}

OPENAI_REPLY = {"created": 1, "data": [{"url": "https://cdn.test/a.png"}]}

ECHO_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'prev': payload.get('_previous_ctx')}
    return {
        'output': [{
            'type': 'message',
            'content': [{'type': 'output_text', 'text': repr(payload.get('prev'))}],
        }],
    }
"""


class _Vendor(BaseHTTPRequestHandler):
    calls: ClassVar[list[dict]] = []
    gets: ClassVar[int] = 0
    reply: ClassVar[dict] = GEMINI_REPLY
    #: Answers with the body it was sent, which is how a script's request phase
    #: can be observed through its own response phase.
    echo: ClassVar[bool] = False

    def do_GET(self):
        type(self).gets += 1
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG_1X1)))
        self.end_headers()
        self.wfile.write(PNG_1X1)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        type(self).calls.append({"path": self.path, "body": body, "raw": raw})
        blob = json.dumps(body if type(self).echo else type(self).reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


@pytest.fixture
def vendor():
    server = HTTPServer(("127.0.0.1", 0), _Vendor)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Vendor.calls = []
    _Vendor.gets = 0
    _Vendor.reply = GEMINI_REPLY
    _Vendor.echo = False
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _headers(
    vendor: str,
    path: str = GEMINI_PATH,
    script: str = "google/images@v1",
    inline: str | None = None,
):
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": vendor + path,
        "X-Auth-Emit": "header:x-goog-api-key",
        "Authorization": "Bearer sk-test",
        "Content-Type": "application/json",
    }
    if inline is None:
        headers["X-Script-Ref"] = script
    else:
        # The channel contract spells inline newlines as a literal backslash-n.
        headers["X-Script"] = inline.replace("\n", "\\n")
    return headers


def _last() -> dict:
    assert _Vendor.calls, "the upstream was never called"
    return _Vendor.calls[-1]


def _parts() -> list:
    return _last()["body"]["contents"][0]["parts"]


# --- chat: the request direction -------------------------------------------


def test_a_chat_message_becomes_the_prompt(client, vendor):
    """The regression: this used to leave `parts[0].text` empty."""
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "messages": [{"role": "user", "content": "写实古风美少女"}]},
    )
    assert resp.status_code == 200, resp.text
    assert _parts()[0] == {"text": "写实古风美少女"}


def test_a_system_turn_is_folded_in_front_of_the_user_turn(client, vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "解读图片"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert _parts()[0] == {"text": "You are a helpful assistant.\n\n解读图片"}


def test_content_parts_fold_into_prompt_and_image_without_fetching(client, vendor):
    url = "https://cdn.example.com/photos/a.jpeg"
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "把背景换成星空"},
                        {"type": "image_url", "image_url": {"url": url}},
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    parts = _parts()
    assert parts[0] == {"text": "把背景换成星空"}
    # camelCase and no download: the reference is forwarded, not fetched.
    assert parts[1] == {"fileData": {"mimeType": "image/jpeg", "fileUri": url}}
    assert _Vendor.gets == 0


def test_a_multi_turn_history_is_truncated_to_the_last_user_turn(client, vendor):
    """Truncation, not refusal: the last user turn carries prompt and image.

    Earlier turns -- assistant text included -- must leave no trace, since the
    canonical contract has one `prompt` and one `image` and anything else would
    reach the vendor as a stray part.
    """
    url = "https://cdn.example.com/photos/a.jpeg"
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "第一轮"},
                    {"type": "image_url",
                     "image_url": {"url": "https://cdn.example.com/first.jpeg"}},
                ]},
                {"role": "assistant", "content": "已生成一张图"},
                {"role": "user", "content": [
                    {"type": "text", "text": "第二轮"},
                    {"type": "image_url", "image_url": {"url": url}},
                ]},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    parts = _parts()
    assert len(parts) == 2
    assert parts[0] == {"text": "第二轮"}
    assert parts[1] == {"fileData": {"mimeType": "image/jpeg", "fileUri": url}}
    assert _Vendor.gets == 0


def test_history_without_a_user_turn_is_refused(client, vendor):
    """An `assistant` turn is skipped as history, so it cannot stand alone."""
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "messages": [{"role": "assistant", "content": "a"}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"


def test_an_unknown_role_is_refused(client, vendor):
    """A misspelled `user` must not pass as history and pick the wrong turn."""
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "messages": [{"role": "usre", "content": "a"}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unsupported_parameter"


def test_an_unknown_content_part_is_refused(client, vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "messages": [{"role": "user",
                          "content": [{"type": "audio", "data": "x"}]}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "messages[0].content[0].type"


def test_missing_messages_is_still_reported_against_messages(client, vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "messages"


# --- chat: the canonical body carries nothing else --------------------------


def test_no_chat_field_reaches_the_upstream(client, vendor):
    """`openai/images@v1` forwards the canonical body verbatim, so a survivor
    would land on the vendor.

    `response_format` is the sharp edge: chat spells it `{"type": ...}`
    (structured output) while the canonical contract only knows "url" and
    "b64_json", so a survivor would fail validation before it ever leaked.
    """
    _Vendor.reply = OPENAI_REPLY
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor, "/v1/images/generations", "openai/images@v1"),
        json={
            "model": "gpt-image-1",
            "messages": [{"role": "user", "content": "a fox"}],
            "temperature": 0.7,
            "max_tokens": 16,
            "tools": [{"type": "function", "function": {"name": "x"}}],
            "response_format": {"type": "json_object"},
            "stream": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = _last()["body"]
    assert body == {"model": "gpt-image-1", "prompt": "a fox"}


# --- chat: the response direction ------------------------------------------


def test_the_reply_is_a_chat_completion_carrying_the_image(client, vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "messages": [{"role": "user", "content": "a fox"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "gemini-3.1-flash-image-preview"
    assert body["id"].startswith("chatcmpl-")
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["role"] == "assistant"
    part = choice["message"]["content"][0]
    assert part["type"] == "image_url"
    # b64_json becomes a data URI, labelled from the bytes rather than guessed
    # from the item, which carries no mime type.
    assert part["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    # usage stays put: the control plane bills from it.
    assert body["usage"]["output_tokens"] == 1120


def test_a_url_reply_is_carried_through_as_a_url(client, vendor):
    _Vendor.reply = OPENAI_REPLY
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor, "/v1/images/generations", "openai/images@v1"),
        json={"model": "gpt-image-1", "messages": [{"role": "user", "content": "a fox"}]},
    )
    assert resp.status_code == 200, resp.text
    part = resp.json()["choices"][0]["message"]["content"][0]
    assert part == {"type": "image_url", "image_url": {"url": "https://cdn.test/a.png"}}


def test_streaming_slices_text_but_sends_the_image_whole(client, vendor):
    resp = client.post(
        "/v1/chat/completions",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "messages": [{"role": "user", "content": "a fox"}], "stream": True},
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert resp.text.rstrip().endswith("data: [DONE]")
    chunks = [json.loads(line[6:]) for line in resp.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # One delta carries the image, in one piece -- it cannot be cut into
    # eight-character slices the way text can.
    carried = [c for c in chunks if isinstance(c["choices"][0]["delta"].get("content"), list)]
    assert len(carried) == 1
    part = carried[0]["choices"][0]["delta"]["content"][0]
    assert part["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    assert PNG_B64 in resp.text


# --- responses -------------------------------------------------------------


def test_responses_bare_parts_are_one_turn_not_several(client, vendor):
    """A bare run of parts is a single turn's parts, so nothing is truncated.

    This is the guard on the unit of truncation. Reading it per item instead of
    per turn would keep the image and drop the instruction here -- a changed
    request, not a shortened one.
    """
    url = "https://cdn.example.com/photos/a.jpeg"
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "input": [
                {"type": "input_text", "text": "把背景换成星空"},
                {"type": "input_image", "image_url": url},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    parts = _parts()
    assert parts[0] == {"text": "把背景换成星空"}
    assert parts[1] == {"fileData": {"mimeType": "image/jpeg", "fileUri": url}}
    assert _Vendor.gets == 0


def test_responses_history_is_truncated_to_the_last_user_turn(client, vendor):
    """Truncation, not refusal: the last user turn carries prompt and image."""
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={
            "model": "gemini-3.1-flash-image-preview",
            "input": [
                {"role": "user", "content": [
                    {"type": "input_text", "text": "第一轮"},
                    {"type": "input_image",
                     "image_url": "https://cdn.example.com/first.jpeg"},
                ]},
                {"role": "assistant", "content": "已生成一张图"},
                {"role": "user", "content": [
                    {"type": "input_text", "text": "第二轮"},
                    {"type": "input_image",
                     "image_url": "https://cdn.example.com/second.jpeg"},
                ]},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    parts = _parts()
    assert len(parts) == 2
    assert parts[0] == {"text": "第二轮"}
    assert parts[1] == {"fileData": {"mimeType": "image/jpeg",
                                     "fileUri": "https://cdn.example.com/second.jpeg"}}
    assert _Vendor.gets == 0


def test_responses_system_item_folds_in_front_of_the_user_turn(client, vendor):
    """The same prompt rule as the chat door, so one rule serves both entries."""
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview", "input": [
            {"role": "system", "content": "只改背景"},
            {"role": "user", "content": "换成星空"},
        ]},
    )
    assert resp.status_code == 200, resp.text
    assert _parts()[0] == {"text": "只改背景\n\n换成星空"}


def test_responses_history_without_a_user_turn_is_refused(client, vendor):
    """An `assistant` item is skipped as history, so it cannot stand alone."""
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "input": [{"role": "assistant", "content": "a"}]},
    )
    # A real user turn is what is missing, not the model: the channel rejects an
    # unknown model with 400 too, which would make this pass for the wrong
    # reason. `code` is what separates the two.
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"


def test_responses_unknown_role_is_refused(client, vendor):
    """A misspelled `user` must not pass as history and pick the wrong turn."""
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview",
              "input": [{"role": "usre", "content": "a"}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "unsupported_parameter"


def test_responses_input_folds_and_output_carries_the_image(client, vendor):
    resp = client.post(
        "/v1/responses",
        headers=_headers(vendor),
        json={"model": "gemini-3.1-flash-image-preview", "input": "a fox",
              "tools": [{"type": "image_generation"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["object"] == "response"
    assert body["id"].startswith("resp-")
    assert body["status"] == "completed"
    call = body["output"][0]
    assert call["type"] == "image_generation_call"
    assert call["status"] == "completed"
    assert call["result"] == PNG_B64
    # `input` folded, and `tools` survived the rebuild -- scripts read it to
    # pick their response modalities.
    assert _parts()[0] == {"text": "a fox"}
    assert _last()["body"]["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]


def test_responses_state_chain_survives_the_rebuild(client, vendor):
    """`_previous_ctx` is the adapter's own hand-off, not a client field.

    A rebuild that dropped it would break the chain silently: the second turn
    would look exactly like a first one, and nothing in the reply would say so.
    The stand-in echoes the script's request phase back, which is the only way
    to see what the script was handed.
    """
    _Vendor.echo = True

    first = client.post(
        "/v1/responses",
        headers=_headers(vendor, "/echo", inline=ECHO_SCRIPT),
        json={"model": "m", "input": "one"},
    )
    assert first.status_code == 200, first.text
    # The echo script answers in the responses shape already, so its own `output`
    # is what the caller sees; the id is what chains the next turn.
    response_id = first.json()["id"]

    second = client.post(
        "/v1/responses",
        headers=_headers(vendor, "/echo", inline=ECHO_SCRIPT),
        json={"model": "m", "input": "two", "previous_response_id": response_id},
    )
    assert second.status_code == 200, second.text
    assert response_id in second.json()["output"][0]["content"][0]["text"]
