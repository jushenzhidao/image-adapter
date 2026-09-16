"""``X-Channel-Options.model_map`` end to end.

Two halves, and both are needed, because each one alone leaves a silent failure
possible:

* **The doors.** The table is a channel header and the rewrite happens in the
  pipeline, so all four entries must behave alike -- they fold into one
  canonical body first (``adapter/api/frontdoor.py``), and the Chat/Responses
  wrappers must keep labelling their reply with the model the caller asked for
  rather than the one the vendor ran.
* **The scripts.** The rewrite only reaches the vendor if the script that owns
  the model honours it. ``openai/images@v1`` forwards the canonical body and
  ``google/images@v1`` reads its model from that body, so the rewrite is the
  whole story for them. ``volcengine_ark/images@v1`` takes its model from
  channel options instead -- its model is an ARK access point id -- and reads
  ``ctx.mapped_model``. That is the case a framework-only implementation gets
  wrong while every unit test stays green, which is why the same assertion is
  made three times, once per shipped script, by ref so the *shipped* file is
  what is exercised.

The stand-in vendors are real sockets: "what did the vendor receive" is not a
question a stub can answer about itself. One records the request body and the
other the URL path, because Google takes the model as a path segment rather
than as a field.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

#: A probe script that reports back what it was handed. It is the only inline
#: script here, and it exists so the door cases measure the pipeline rather than
#: a vendor script's own model handling. `model` is echoed as None when the key
#: is absent, which is how "no model at all" is told from an empty one.
PROBE = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'model': payload.get('model'), 'prompt': payload.get('prompt')}
    return {'created': 1, 'data': [{'url': 'https://cdn.vendor.test/a.png'}]}
"""

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)

MAPPED = "doubao-seedream-5-0-pro-260628"
CLIENT_MODEL = "gpt-image-2"
ARK_DEFAULT = "doubao-seedream-5-0-260128"

GEMINI_ENDPOINT = "/v1beta/models/gemini-2.5-flash-image:generateContent"


class _Recorder(BaseHTTPRequestHandler):
    """Records what it was asked for, and answers in the canonical shape."""

    bodies: ClassVar[list[dict]] = []
    paths: ClassVar[list[str]] = []

    def do_POST(self):  # noqa: N802 -- the stdlib's spelling
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        type(self).paths.append(self.path)
        type(self).bodies.append(json.loads(raw or b"{}"))
        payload = json.dumps(
            {"created": 1, "data": [{"url": "https://cdn.vendor.test/a.png"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class _Gemini(_Recorder):
    """The same, with a reply Google's script can turn into an image."""

    def do_POST(self):  # noqa: N802 -- the stdlib's spelling
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        type(self).paths.append(self.path)
        type(self).bodies.append(json.loads(raw or b"{}"))
        payload = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(PNG_1X1).decode(),
                                    }
                                }
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"totalTokenCount": 10},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def vendor():
    _Recorder.bodies = []
    _Recorder.paths = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1/images/generations"
    server.shutdown()
    server.server_close()


@pytest.fixture
def gemini():
    _Gemini.bodies = []
    _Gemini.paths = []
    server = HTTPServer(("127.0.0.1", 0), _Gemini)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _options(table: object) -> dict:
    """The header block every case in this file varies, and nothing else."""
    return {"X-Channel-Options": json.dumps({"model_map": table})}


def _posted_model() -> object:
    assert _Recorder.bodies, "the upstream was never called"
    return _Recorder.bodies[-1].get("model")


# --- the doors ---------------------------------------------------------------


@pytest.mark.parametrize("door", ["generations", "edits", "chat", "responses"])
def test_every_door_sends_the_mapped_model(client, channel_headers, vendor, door):
    """One table, four entries, one answer.

    The two text doors do not carry a canonical body of their own -- they are
    folded into one by ``frontdoor.py`` before the pipeline sees them -- so a
    mapping implemented against the canonical shape is easy to lose on the way
    in. That is what this parametrisation is for.
    """
    headers = channel_headers(PROBE, vendor, **_options({"*": MAPPED}))
    body = {"model": CLIENT_MODEL, "prompt": "a fox"}

    if door == "generations":
        resp = client.post("/v1/images/generations", headers=headers, json=body)
    elif door == "edits":
        # The multipart Content-Type (with its boundary) is the client's to set,
        # so the JSON one comes off first -- exactly as the edits suite does it.
        headers.pop("Content-Type", None)
        resp = client.post(
            "/v1/images/edits",
            headers=headers,
            files={"image": ("a.png", io.BytesIO(PNG_1X1), "image/png")},
            data=body,
        )
    elif door == "chat":
        resp = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": CLIENT_MODEL,
                "messages": [{"role": "user", "content": "a fox"}],
            },
        )
    else:
        resp = client.post(
            "/v1/responses",
            headers=headers,
            json={"model": CLIENT_MODEL, "input": "a fox"},
        )

    assert resp.status_code == 200, resp.text
    assert _posted_model() == MAPPED


def test_chat_labels_the_reply_with_the_model_the_caller_asked_for(
    client, channel_headers, vendor
):
    """The mapping is the channel's; the name is the caller's.

    A reply labelled ``doubao-...`` to a caller that asked for ``gpt-image-2``
    leaks the upstream's name into a client-visible field -- and it is exactly
    what a naive "rewrite the body" would produce, since Chat answers from the
    canonical body it folded.
    """
    resp = client.post(
        "/v1/chat/completions",
        headers=channel_headers(PROBE, vendor, **_options({"*": MAPPED})),
        json={
            "model": CLIENT_MODEL,
            "messages": [{"role": "user", "content": "a fox"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == CLIENT_MODEL
    assert MAPPED not in json.dumps(resp.json())


def _send_image_request(client, channel_headers, vendor, table, model):
    """One canonical request through the images door, table and model varied."""
    body: dict = {"prompt": "a fox"}
    if model is not None:
        body["model"] = model
    resp = client.post(
        "/v1/images/generations",
        headers=channel_headers(PROBE, vendor, **_options(table)),
        json=body,
    )
    assert resp.status_code == 200, resp.text
    return _posted_model()


def test_no_table_leaves_the_model_alone(client, channel_headers, vendor):
    """The default: a channel that declares nothing is unchanged."""
    sent = client.post(
        "/v1/images/generations",
        headers=channel_headers(PROBE, vendor),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert sent.status_code == 200, sent.text
    assert _posted_model() == CLIENT_MODEL


def test_an_unlisted_model_is_sent_as_it_arrived(client, channel_headers, vendor):
    """The table translates; it does not filter."""
    assert (
        _send_image_request(
            client, channel_headers, vendor, {CLIENT_MODEL: MAPPED}, "gpt-image-1"
        )
        == "gpt-image-1"
    )


def test_an_exact_key_beats_the_wildcard(client, channel_headers, vendor):
    table = {CLIENT_MODEL: "exact-wins", "*": MAPPED}
    assert (
        _send_image_request(client, channel_headers, vendor, table, CLIENT_MODEL)
        == "exact-wins"
    )
    assert (
        _send_image_request(client, channel_headers, vendor, table, "other") == MAPPED
    )


def test_the_wildcard_catches_a_request_that_sent_no_model(
    client, channel_headers, vendor
):
    """`{"*": X}` means "this channel speaks X", so a nameless request gets it.

    The alternative -- leaving the field absent because the client said nothing
    -- would hand the vendor "no model", which is the one thing a channel that
    fronts a single model exists to avoid.
    """
    assert (
        _send_image_request(client, channel_headers, vendor, {"*": MAPPED}, None)
        == MAPPED
    )


@pytest.mark.parametrize(
    "table",
    [
        ["not", "an", "object"],
        "not-an-object",
        7,
        {"*": 7},
        {"*": "   "},
        {"": MAPPED},
        {"gemini-*": MAPPED},
    ],
    ids=[
        "list",
        "string",
        "number",
        "non-string-value",
        "blank-value",
        "blank-key",
        "partial-glob",
    ],
)
def test_an_unusable_table_is_refused_before_any_upstream_call(
    client, channel_headers, vendor, table
):
    """A declared knob the adapter cannot honour must not look like one it did.

    All seven shapes are the same mistake from the operator's side -- a table
    they believe is in effect. The last one is the interesting case: a partial
    glob reads like a pattern and would silently never match, which is worse
    than refusing it outright.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=channel_headers(PROBE, vendor, **_options(table)),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "channel_config_error"
    assert error["param"] == "X-Channel-Options"
    assert "model_map" in error["message"]
    assert _Recorder.bodies == [], "the request reached the upstream anyway"


def test_a_repeated_key_on_the_wire_is_refused(client, channel_headers, vendor):
    """`{"*": "a", "*": "b"}` must not read as a one-entry table.

    Every JSON parser keeps the last duplicate and says nothing, so before this
    refusal the operator's second value won silently -- the same "the table says
    something else than what I wrote" defect the rest of these cases exist for.
    """
    raw = '{"model_map": {"*": "first-wins", "*": "second-wins"}}'
    resp = client.post(
        "/v1/images/generations",
        headers=channel_headers(PROBE, vendor, **{"X-Channel-Options": raw}),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "channel_config_error"
    assert error["param"] == "X-Channel-Options"
    assert "repeats" in error["message"]
    assert _Recorder.bodies == [], "the request reached the upstream anyway"


def test_the_mapping_runs_after_admission(client, channel_headers, vendor):
    """The table rides in the same header block as everything else.

    Not a mapping test so much as a check on ordering: parsing it must not
    give an unauthenticated caller anything they did not have before.
    """
    headers = channel_headers(PROBE, vendor, **_options({"*": MAPPED}))
    headers["X-Adapter-Key"] = "wrong"
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 401
    assert _Recorder.bodies == []


# --- the scripts -------------------------------------------------------------


def _ref_headers(url: str, ref: str, table: object | None = None) -> dict:
    """The channel New API would send for a script that lives in the store.

    Spelled out rather than borrowed from the `channel_headers` fixture, which
    always sets X-Script: a channel uses one source or the other, and setting
    both would be refused before the test could say anything about mapping.
    """
    headers = {
        "X-Adapter-Key": "test-adapter-key",
        "X-Upstream-Url": url,
        "X-Script-Ref": ref,
        "Content-Type": "application/json",
    }
    if table is not None:
        headers.update(_options(table))
    return headers


def test_openai_forwards_the_mapped_model(client, vendor):
    """`openai/images@v1` never reads `model`: it forwards the canonical body.

    Which is why the rewrite has to happen before the script runs -- there is
    no line in that file to change.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=_ref_headers(vendor, "openai/images@v1", {"*": MAPPED}),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _posted_model() == MAPPED

    # And without a table the client's own name still travels, which is what
    # "passthrough" means for the one script that has no model opinion at all.
    assert (
        client.post(
            "/v1/images/generations",
            headers=_ref_headers(vendor, "openai/images@v1"),
            json={"model": CLIENT_MODEL, "prompt": "a fox"},
        ).status_code
        == 200
    )
    assert _posted_model() == CLIENT_MODEL


def test_google_sends_the_mapped_model_in_the_path(client, gemini):
    """Google takes the model as a path segment, and the mapping still applies.

    The mapped name then goes through the capability table, so the two stages
    compose: the channel table says what this channel is asked for, and the
    vendor table resolves what the vendor calls it.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=_ref_headers(
            f"{gemini}{GEMINI_ENDPOINT}",
            "google/images@v1",
            {"*": "gemini-3.1-flash-image"},
        ),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _Gemini.paths[-1] == "/v1beta/models/gemini-3.1-flash-image:generateContent"


def test_google_resolves_a_mapped_alias(client, gemini):
    """Mapping to an alias still lands on the real id: the stages compose."""
    resp = client.post(
        "/v1/images/generations",
        headers=_ref_headers(
            f"{gemini}{GEMINI_ENDPOINT}",
            "google/images@v1",
            {"*": "nano-banana-2"},
        ),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _Gemini.paths[-1] == "/v1beta/models/gemini-3.1-flash-image:generateContent"


def test_ark_prefers_the_mapping(client, vendor):
    """The one script the rewrite alone does not reach.

    ARK's model is an access point id, so this script always had its own source
    for it (``ctx.options['model']``, then the shipped default) and ignored the
    body. A framework-only implementation leaves this case sending the wrong
    model with every unit test green, which is why it is asserted here.
    """
    resp = client.post(
        "/v1/images/generations",
        headers=_ref_headers(
            vendor,
            "volcengine_ark/images@v1",
            {CLIENT_MODEL: MAPPED},
        ),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _posted_model() == MAPPED


def test_ark_keeps_its_channel_model_when_nothing_matches(client, vendor):
    """No match means no change -- including the one script that owns a model.

    This is the zero-regression promise in the shape it takes for ARK: an
    unmatched request must still be sent as the channel's access point, not as
    whatever routing label the caller used.
    """
    headers = _ref_headers(vendor, "volcengine_ark/images@v1")
    headers["X-Channel-Options"] = json.dumps(
        {"model": ARK_DEFAULT, "model_map": {"some-other-model": MAPPED}}
    )
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _posted_model() == ARK_DEFAULT


def test_ark_falls_back_to_its_builtin_default_without_any_option(client, vendor):
    """No table and no `model` option: the shipped default, exactly as before."""
    resp = client.post(
        "/v1/images/generations",
        headers=_ref_headers(vendor, "volcengine_ark/images@v1"),
        json={"model": CLIENT_MODEL, "prompt": "a fox"},
    )
    assert resp.status_code == 200, resp.text
    assert _posted_model() == ARK_DEFAULT
