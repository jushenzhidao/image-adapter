"""tencent_ci 渠道的「多形态」端到端测试：四个前门 × 输入/输出形态。

覆盖矩阵（全部走 TestClient + 真 lifespan + 真 script_store 链路 + 假 COS）：

  * 门：/v1/images/generations、/v1/images/edits（multipart）、
        /v1/chat/completions、/v1/responses —— 验证折叠门各自留下什么
        （generations 门原样透传 vendor extras；edits/chat/responses 门
        白名单只留 model/prompt/image，op 只能靠 X-Model-Map 或
        X-Channel-Options.op 存活）；
  * 输入形态：URL 直通（URL 指向假 COS 自身——脚本的预检会先下载探测
    字节，所以引用图必须可达）、data URI（经对象存储转存）、multipart
    上传文件；
  * 输出形态：默认 url（转存链接）、b64_json；
  * 操作解析：model 原文白名单、X-Model-Map 映射、X-Channel-Options.op，
    以及三者全无时的 400；`size` 档位 → magnify 推导；
  * 错误形态：COS 403 → upstream_http_error（XML detail 在引擎处丢失，
    这是已记录的框架缺口，测试钉住的是「请求方看到什么」）。

零消耗：假 COS 不计费、FakeStorage 不出网，detect-url 指向的假 COS 对象
没有谁真的去拉。
"""

from __future__ import annotations

import base64
import io
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image
from starlette.testclient import TestClient

from adapter.settings import Settings
from tests.integration.conftest import FakeStorage

SECRET_ID = "AKIDintegrationTest"
SECRET_KEY = "integration-secret-key"


def _probe_png(width: int = 64, height: int | None = None) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height or width), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


#: 一张真实的 64×64 PNG——过了上游 32px 下限，预检放行。
PROBE_PNG = _probe_png()
PROBE_DATA_URI = "data:image/png;base64," + base64.b64encode(PROBE_PNG).decode()

COS_403_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<Error><Code>SignatureDoesNotMatch</Code>"
    b"<Message>The request signature does not conform to COS standards.</Message></Error>"
)


class _FakeCOS(BaseHTTPRequestHandler):
    """Records every call; answers 200 image/png (or a programmed failure).

    The programmed failure fires only for `ci-process` calls: a bare GET of
    the probe object is the script's *preflight download* and must succeed.
    """

    def do_GET(self):
        parsed = urlparse(self.path)
        self.server.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query, keep_blank_values=True),
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        if self.server.fail_status and "ci-process" in parsed.query:
            body = self.server.fail_body
            self.send_response(self.server.fail_status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PROBE_PNG)))
        self.end_headers()
        self.wfile.write(PROBE_PNG)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_cos():
    server = HTTPServer(("127.0.0.1", 0), _FakeCOS)
    server.requests = []
    server.fail_status = 0
    server.fail_body = b""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _cos_url(fake_cos) -> str:
    return f"http://127.0.0.1:{fake_cos.server_port}/"


def _ref_url(fake_cos) -> str:
    """一个可达的引用图 URL：就是假 COS 自己（预检下载 + 上游拉取都打它）。"""
    return f"http://127.0.0.1:{fake_cos.server_port}/probe-src/dog.png"


def _ci_headers(fake_cos, **extra) -> dict[str, str]:
    """The channel directive exactly as New API would send it.

    No Content-Type here: httpx sets it per body (json= vs multipart), and a
    hand-pinned one would clobber the multipart boundary and leave the edits
    door parsing an empty form.
    """
    headers = {
        "X-Upstream-Url": _cos_url(fake_cos),
        "X-Script-Ref": "tencent_ci/images@v1",
        "X-Auth-Emit": "none",
        "Authorization": f"{SECRET_ID}|{SECRET_KEY}",
    }
    headers.update(extra)
    return headers


def _last(fake_cos) -> dict:
    assert fake_cos.requests, "the fake COS saw no call at all"
    return fake_cos.requests[-1]


# --------------------------------------------------------------- generations


def test_generations_url_reference_with_verbatim_model(client, storage, fake_cos):
    """model 原文命中操作名白名单 ⇒ 免映射直用；URL 直通；产出转存链接。"""
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={"model": "GoodsMatting", "prompt": "", "image": _ref_url(fake_cos)},
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["method"] == "GET"
    assert call["query"]["ci-process"] == ["GoodsMatting"]
    assert call["query"]["detect-url"] == [_ref_url(fake_cos)]  # parse_qs 已解码
    # 签名头就位，且 X-Auth-Emit: none 压住了引擎默认的 Bearer 发射
    assert call["headers"]["authorization"].startswith("q-sign-algorithm=sha1")
    assert "Bearer" not in call["headers"]["authorization"]
    # 结果是转存链接（假 COS 不产链接，url 必出自 FakeStorage）
    item = resp.json()["data"][0]
    assert item["url"].startswith("https://cdn.test/")
    assert item["width"] == 64 and item["height"] == 64


def test_generations_model_map_and_inline_reference_and_magnify(
    client, storage, fake_cos
):
    """X-Model-Map 把网关模型名映射成操作；data URI 先转存；magnify 透传。"""
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(
            fake_cos, **{"X-Model-Map": "gpt-image-2=AISuperResolution"}
        ),
        json={
            "model": "gpt-image-2",
            "prompt": "",
            "image": PROBE_DATA_URI,
            "magnify": 4,
        },
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["AISuperResolution"]
    assert call["query"]["magnify"] == ["4"]
    detect_url = call["query"]["detect-url"][0]
    assert detect_url.startswith("https://cdn.test/")  # 转存链接，非 data URI
    assert storage.puts and storage.puts[0][2] == PROBE_PNG  # 转存的正是原图字节


def test_generations_size_tier_derives_magnify(client, storage, fake_cos):
    """size=2k + 64px 输入 ⇒ 推导出 magnify=4（最近可达档）。"""
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={"model": "AISuperResolution", "prompt": "", "image": PROBE_DATA_URI,
              "size": "2k"},
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["AISuperResolution"]
    assert call["query"]["magnify"] == ["4"]


def test_generations_b64_json_output(client, storage, fake_cos):
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={
            "model": "AIPortraitMatting",
            "prompt": "",
            "image": _ref_url(fake_cos),
            "response_format": "b64_json",
        },
    )
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["data"]
    assert "url" not in item
    assert base64.b64decode(item["b64_json"]) == PROBE_PNG


def test_generations_layout_extras_pass_through(client, storage, fake_cos):
    """generations 门不折叠 vendor extras：抠图布局参数直达 query。"""
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={
            "model": "GoodsMatting",
            "prompt": "",
            "image": _ref_url(fake_cos),
            "center-layout": 1,
            "padding-layout": "20x10",
        },
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["center-layout"] == ["1"]
    assert call["query"]["padding-layout"] == ["20x10"]


def test_generations_without_image_is_a_400(client, storage, fake_cos):
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={"model": "AISuperResolution", "prompt": "把图放大"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "image"


# --------------------------------------------------------------------- edits


def test_edits_door_multipart_op_survives_via_channel_options(
    client, storage, fake_cos
):
    """edits 门是**透传门**（表单字段全进 canonical body）；op 不是 canonical
    字段，只能靠 X-Channel-Options 存活——这正是它走渠道头的原因。"""
    resp = client.post(
        "/v1/images/edits",
        headers=_ci_headers(
            fake_cos,
            **{"X-Channel-Options": '{"op": "GoodsMatting", "center-layout": 1}'},
        ),
        data={"prompt": "把商品抠出来"},
        files={"image": ("goods.png", PROBE_PNG, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["GoodsMatting"]
    assert call["query"]["center-layout"] == ["1"]
    # 上传的文件字节经转存后作为 detect-url
    assert call["query"]["detect-url"][0].startswith("https://cdn.test/")
    assert storage.puts and storage.puts[0][2] == PROBE_PNG


def test_edits_door_form_size_passes_through(client, storage, fake_cos):
    """size 是普通表单字段：edits 门原样透传，不需要任何渠道头。"""
    resp = client.post(
        "/v1/images/edits",
        headers=_ci_headers(fake_cos),
        data={
            "prompt": "2k",
            "model": "AISuperResolution",
            "size": "2k",
            "response_format": "b64_json",
        },
        files={"image": ("t.png", PROBE_PNG, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["AISuperResolution"]
    assert call["query"]["magnify"] == ["4"]  # 64px 输入问 2K → 最近可达档
    (item,) = resp.json()["data"]
    assert base64.b64decode(item["b64_json"]) == PROBE_PNG


# ---------------------------------------------------------------------- chat


def test_chat_door_folds_history_and_keeps_model(client, storage, fake_cos):
    """chat 门：截断到最后 user 轮、model 保留、响应按 chat 形态包回。"""
    resp = client.post(
        "/v1/chat/completions",
        headers=_ci_headers(fake_cos),
        json={
            "model": "GoodsMatting",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _ref_url(fake_cos)}}
                    ],  # 历史输出，应被忽略
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _ref_url(fake_cos)}},
                        {"type": "text", "text": "把主体抠出来"},
                    ],
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["GoodsMatting"]
    assert call["query"]["detect-url"] == [_ref_url(fake_cos)]
    body = resp.json()
    part = body["choices"][0]["message"]["content"][0]
    assert part["image_url"]["url"].startswith("https://cdn.test/")


# ----------------------------------------------------------------- responses


def test_responses_door_image_part(client, storage, fake_cos):
    resp = client.post(
        "/v1/responses",
        headers=_ci_headers(fake_cos),
        json={
            "model": "AIPicMatting",
            "input": [
                {"type": "input_image", "image_url": _ref_url(fake_cos)},
                {"type": "input_text", "text": "通用抠图"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    call = _last(fake_cos)
    assert call["query"]["ci-process"] == ["AIPicMatting"]
    body = resp.json()
    assert body["output"][0]["result"].startswith("https://cdn.test/")


def test_generations_short_circuits_when_input_meets_size(
    client, storage, fake_cos
):
    """2048×1152 输入问 2k：原图直出——上游零调用、存储零写入、零计费。"""
    big = _probe_png(2048, 1152)
    big_uri = "data:image/png;base64," + base64.b64encode(big).decode()
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={
            "model": "AISuperResolution",
            "prompt": "",
            "image": big_uri,
            "size": "2k",
            "response_format": "b64_json",
        },
    )
    assert resp.status_code == 200, resp.text
    assert fake_cos.requests == []  # 零上游调用：引擎根本没出网
    assert storage.puts == []  # b64 形态不写存储
    (item,) = resp.json()["data"]
    assert base64.b64decode(item["b64_json"]) == big  # 产物 == 输入字节


def test_short_circuit_with_x_async_is_a_config_error(client, storage, fake_cos):
    headers = _ci_headers(fake_cos, **{"X-Async": "poll=1,timeout=60"})
    resp = client.post(
        "/v1/images/generations",
        headers=headers,
        json={
            "model": "AISuperResolution",
            "prompt": "",
            "image": PROBE_DATA_URI,
            "size": "64x64",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "channel_config_error"
    assert fake_cos.requests == []  # 配置错误在发上游前被拒


# ---------------------------------------------------------------- 错误形态


def test_no_operation_anywhere_is_a_loud_400(client, storage, fake_cos):
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={"model": "gpt-image-2", "prompt": "", "image": _ref_url(fake_cos)},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "channel_config_error"
    assert "AISuperResolution" in error["message"]  # 报错里列出合法操作名


def test_two_images_are_refused(client, storage, fake_cos):
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={
            "model": "GoodsMatting",
            "prompt": "",
            "image": [_ref_url(fake_cos), _ref_url(fake_cos)],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["param"] == "image"


def test_upstream_403_becomes_upstream_http_error(client, storage, fake_cos):
    """COS 拒签 ⇒ 引擎 raise_for_status 兜住；XML detail 丢失是已知框架缺口。"""
    fake_cos.fail_status = 403
    fake_cos.fail_body = COS_403_XML
    resp = client.post(
        "/v1/images/generations",
        headers=_ci_headers(fake_cos),
        json={"model": "GoodsMatting", "prompt": "", "image": _ref_url(fake_cos)},
    )
    assert resp.status_code == 400  # 4xx → 400（签名/权限类错误不重试）
    error = resp.json()["error"]
    assert error["code"] == "upstream_http_error"
    assert "403" in error["message"]
