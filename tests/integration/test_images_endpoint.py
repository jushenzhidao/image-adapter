"""Integration tests for /v1/images/generations under the channel contract.

Model resolution no longer happens here (the control plane owns it), so the
old model_not_found cases are gone. What remains: payload shape validation
and channel header validation.
"""

from __future__ import annotations

IMAGES_SCRIPT = """
async def transform(ctx, payload, phase):
    if phase == 'request':
        return {'desc': payload.get('prompt', '')}
    return {'data': [{'url': 'https://cdn.vendor.test/img.png'}]}
"""

FAKE_UPSTREAM = "https://vendor.test/v2/text2img"


def test_images_validation_bad_n(client, channel_headers):
    resp = client.post(
        "/v1/images/generations",
        headers=channel_headers(IMAGES_SCRIPT, FAKE_UPSTREAM),
        json={"prompt": "cat", "n": 0},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "n"


def test_images_validation_bad_response_format(client, channel_headers):
    resp = client.post(
        "/v1/images/generations",
        headers=channel_headers(IMAGES_SCRIPT, FAKE_UPSTREAM),
        json={"prompt": "cat", "response_format": "hologram"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "response_format"


def test_images_requires_script_header(client, channel_headers):
    headers = channel_headers(IMAGES_SCRIPT, FAKE_UPSTREAM)
    headers.pop("X-Script")
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "channel_config_error"


def test_images_rejects_two_script_sources(client, channel_headers):
    headers = channel_headers(IMAGES_SCRIPT, FAKE_UPSTREAM)
    headers["X-Script-Ref"] = "vendor/x@v1"
    resp = client.post(
        "/v1/images/generations", headers=headers, json={"prompt": "cat"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "channel_config_error"
