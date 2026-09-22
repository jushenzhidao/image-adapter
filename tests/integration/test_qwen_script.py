"""qwen/images@v1 through real sockets: do the three phases actually reach the wire?

Three things a unit test cannot answer, all assembly-shaped (the script can be
correct and the wiring still drop its output):

  1. the fingerprint headers emitted by the *auth* phase arrive at the vendor
     (Cookie, bx-ua, bx-umidtoken, version, Sec-Fetch-*) -- without them the
     real upstream answers with a WAF challenge page, and no unit test sees
     the wire;
  2. the free `chats/new` happens exactly once, and the generation call is
     addressed to the chat_id it returned (ctx.emit(url=..., query=...));
  3. an SSE reply -- which parse_body refuses to decode -- still reaches the
     script as ctx.upstream_raw and comes back out as an OpenAI payload.

The quota case is the differential partner of the happy path: same request,
vendor answers a JSON error frame instead of a stream, and the client must see
429 upstream_quota_exhausted rather than a 502 or a silent 200.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


CREDENTIALS = {"cookie": "a=1", "bx_ua": "234!x", "bx_umidtoken": "T2gAx",
               "chat_mode": "guest"}

CHAT_OK = {"success": True, "data": {"id": "chat-9"}}
SSE_OK = (
    'data: {"choices":[{"delta":{"content":'
    '"https://cdn.qwenlm.ai/fake/1.png"}}]}\n\n'
)
QUOTA_JSON = {"success": False,
              "data": {"code": "RateLimited",
                       "details": "今日生图额度已用完，登录后可继续生图。"}}


class _QwenVendor(BaseHTTPRequestHandler):
    """Stands in for chat.qwen.ai: session mint, OSS credential + object PUT, SSE.

    The upload leg is real too. `getstsToken` answers an endpoint of
    `http://localhost:<this port>` with a bucket name, so the script's
    `PutObject` URL is `http://b.localhost:<port>/...` -- which resolves to
    loopback (RFC 6761; `*.localhost` is loopback on macOS and on systemd
    hosts). That is what makes the edit links testable end to end instead of
    only against a stubbed client.
    """

    mode = "ok"                     # "ok" | "quota" | "waf"
    chats_new: list[dict] = []      # {"headers", "body"}
    generations: list[dict] = []    # {"headers", "query", "body"}
    sts: list[dict] = []            # {"headers", "body"}  (getstsToken)
    uploads: list[dict] = []        # {"path", "headers", "body"}  (OSS PutObject)
    lock = threading.Lock()

    @classmethod
    def reset(cls, mode: str = "ok") -> None:
        cls.mode = mode
        cls.chats_new = []
        cls.generations = []
        cls.sts = []
        cls.uploads = []

    def _reply(self, raw: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def do_POST(self):
        if self.path.startswith("/api/v2/chats/new"):
            with self.lock:
                type(self).chats_new.append({
                    "headers": dict(self.headers.items()),
                    "body": self._read_body(),
                })
            if type(self).mode == "waf":
                self._reply(b"<!doctype html><meta name=\"aliyun_waf_aa\">",
                            "text/html")
                return
            self._reply(json.dumps(CHAT_OK).encode(), "application/json")
            return

        if self.path.startswith("/api/v2/files/getstsToken"):
            with self.lock:
                type(self).sts.append({
                    "headers": dict(self.headers.items()),
                    "body": self._read_body(),
                })
                n = len(type(self).sts)
            port = self.server.server_port
            doc = {"success": True, "data": {
                "access_key_id": "ak-" + str(n),
                "access_key_secret": "sk-" + str(n),
                "security_token": "sts-" + str(n),
                "bucketname": "b",
                "endpoint": "http://localhost:" + str(port),
                # 序号进路径与 URL：多图用例据此证明"保序"
                "file_path": "webui/upload/" + str(n) + ".png",
                "file_url": "https://cdn.example/upload/" + str(n) + ".png",
                "file_id": "fid-" + str(n),
            }}
            self._reply(json.dumps(doc).encode(), "application/json")
            return

        if self.path.startswith("/api/v2/chat/completions"):
            with self.lock:
                type(self).generations.append({
                    "headers": dict(self.headers.items()),
                    "query": self.path.split("?", 1)[1] if "?" in self.path else "",
                    "body": self._read_body(),
                })
            if type(self).mode == "quota":
                self._reply(json.dumps(QUOTA_JSON).encode(), "application/json")
                return
            self._reply(SSE_OK.encode(), "text/event-stream")
            return

        self._reply(b"not found", "text/plain", 404)

    def do_PUT(self):
        """PutObject. Records what landed, so the tests can prove order and count."""
        with self.lock:
            type(self).uploads.append({
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": self._read_body(),
            })
        self._reply(b"", "application/xml")

    def log_message(self, *args):
        pass


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def vendor():
    _QwenVendor.reset("ok")
    server, base = _serve(_QwenVendor)
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _wildcard_localhost_resolves(monkeypatch):
    """`*.localhost` 是回环（RFC 6761），但这台测试机的 resolver 不认。

    实测 `socket.getaddrinfo("b.localhost", 80)` -> `gaierror`（`localhost` 本身正常），
    而假上游按 OSS 的 virtual-host 风格把 bucket 拼在域名前（`b.localhost:<port>`）
    ⇒ 本机跑这里会得到 502「Cannot connect to host b.localhost」，看起来像上传链路
    的代码缺陷，实际是环境差异。只把**解析**这一步补回回环：连接、V4 签名、
    PutObject 的内容与顺序都仍走真实路径（不 stub 客户端，也不改生产代码）。
    """
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith(".localhost"):
            host = "127.0.0.1"
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def _headers(vendor: str, options: dict | None = None) -> dict[str, str]:
    headers = {
        "X-Upstream-Url": f"{vendor}/api/v2/chat/completions",
        "X-Script-Ref": "qwen/images@v1",
        "Content-Type": "application/json",
    }
    if options is not None:
        headers["X-Channel-Options"] = json.dumps(options)
    return headers


def test_full_link_reaches_the_wire(client, vendor):
    _QwenVendor.reset("ok")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "a cat", "size": "2K"},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/fake/1.png"

    # 1) the free session mint happened exactly once, with the fingerprint
    #    headers attached (ctx.http used the auth-phase header set)
    assert len(_QwenVendor.chats_new) == 1
    mint = _QwenVendor.chats_new[0]["headers"]
    assert mint.get("Cookie") == CREDENTIALS["cookie"]
    assert mint.get("bx-ua") == CREDENTIALS["bx_ua"]
    assert mint.get("bx-umidtoken") == CREDENTIALS["bx_umidtoken"]
    assert mint.get("version") == "0.2.0"
    assert mint.get("Sec-Fetch-Mode") == "cors"

    # 2) the generation call is addressed to the minted chat_id
    assert len(_QwenVendor.generations) == 1
    gen = _QwenVendor.generations[0]
    assert "chat_id=chat-9" in gen["query"]
    for name, value in (("Cookie", CREDENTIALS["cookie"]),
                        ("bx-ua", CREDENTIALS["bx_ua"]),
                        ("bx-umidtoken", CREDENTIALS["bx_umidtoken"]),
                        ("version", "0.2.0"),
                        ("Sec-Fetch-Mode", "cors"),
                        ("sec-ch-ua-platform", '"macOS"')):
        assert gen["headers"].get(name) == value, name
    body = json.loads(gen["body"])
    assert body["chat_id"] == "chat-9" and body["chat_mode"] == "guest"
    assert body["messages"][0]["extra"]["meta"]["model"] == "qwen-image-3.0-pro"
    # 3) the SSE bytes reached the script and came back as an OpenAI payload --
    #    asserted by out["data"][0]["url"] above, which only exists if the
    #    ctx.upstream_raw seam carried the stream.


def test_quota_error_surfaces_as_429_quota_exhausted(client, vendor):
    _QwenVendor.reset("quota")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "a cat"},
    )
    assert resp.status_code == 429, resp.text
    err = resp.json()["error"]
    assert err["code"] == "upstream_quota_exhausted"
    assert "额度已用完" in err["message"]


def test_waf_challenge_page_surfaces_as_upstream_error(client, vendor):
    _QwenVendor.reset("waf")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "a cat"},
    )
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["code"] == "upstream_error"
    # 断言按行为而非句子：脚本后来能**点名**挑战页（早期是笼统的 "non-JSON" 分支），
    # 而运维真正需要的是"这是 WAF 挑战页、要换出口或等冷却"，不是"解析失败"。
    assert "WAF" in err["message"]
    assert "换出口" in err["message"] or "冷却" in err["message"]


# ------------------------------------------------- 渠道密钥（Bearer）当凭据

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6IjUxN2IifQ.sig"


def test_channel_key_becomes_the_cookie_token_on_the_wire(client, vendor):
    """密钥走 Authorization 进来，出去必须是 Cookie —— 上游认的是后者（§2.1）。"""
    _QwenVendor.reset("ok")
    headers = _headers(vendor, {})          # 不配 cookie，只有密钥
    headers["Authorization"] = "Bearer " + JWT
    resp = client.post(
        "/v1/images/generations", headers=headers,
        json={"model": "qwen-image", "prompt": "a cat"},
    )
    assert resp.status_code == 200, resp.text

    mint = _QwenVendor.chats_new[0]["headers"]
    assert mint.get("Cookie") == "token=" + JWT
    assert json.loads(_QwenVendor.chats_new[0]["body"])["chat_mode"] == "normal"
    gen = _QwenVendor.generations[0]
    assert gen["headers"].get("Cookie") == "token=" + JWT
    # 框架默认还会把密钥发成 `Authorization: Bearer <key>`；这一行是提醒它同时
    # 存在于线上。不想要它就把渠道配成 `X-Auth-Emit: none`（下一个用例）。
    assert gen["headers"].get("Authorization") == "Bearer " + JWT


def test_auth_emit_none_keeps_the_token_out_of_a_non_browser_header(client, vendor):
    """抓包里没有 Authorization ⇒ 可以要求引擎不要发它（X-Auth-Emit: none）。"""
    _QwenVendor.reset("ok")
    headers = _headers(vendor, {})
    headers["Authorization"] = "Bearer " + JWT
    headers["X-Auth-Emit"] = "none"
    resp = client.post(
        "/v1/images/generations", headers=headers,
        json={"model": "qwen-image", "prompt": "a cat"},
    )
    assert resp.status_code == 200, resp.text
    gen = _QwenVendor.generations[0]
    assert "Authorization" not in gen["headers"]
    assert gen["headers"].get("Cookie") == "token=" + JWT


# ---------------------------------------- 三条生图形态：文生 / 图生 / 多图生

#: 1×1 PNG —— 真图片字节，让门面的 MIME 嗅探与 data URI 改写走真实路径。
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


def _png(name="a.png"):
    return (name, io.BytesIO(PNG_1X1), "image/png")


def _edit_headers(vendor: str, options: dict | None = None) -> dict[str, str]:
    """multipart 请求：Content-Type 必须由 requests 自己带 boundary，不能预先写死。"""
    headers = _headers(vendor, options)
    headers.pop("Content-Type", None)
    return headers


def _uploads_by_path() -> dict:
    """上传路径 -> 它承载的字节。

    🔴 **到达顺序不再是可观测量**（2026-09-19 垫图并发化）：假上游按**请求到达顺序**
    发号（`1.png`、`2.png`…），并发下"哪张图拿到哪个号"就不确定了。但
    `files[i]` 与"它上传的字节"之间的对应**始终由脚本保证**，所以把两者映起来比字节，
    既恢复顺序断言、又不依赖任何时序。
    """
    return {u["path"].rsplit("/", 1)[-1]: u["body"] for u in _QwenVendor.uploads}


def _msg() -> dict:
    """最近一次生成调用里那条用户消息。"""
    return json.loads(_QwenVendor.generations[-1]["body"])["messages"][0]


def test_text_to_image_uses_the_t2i_link_and_uploads_nothing(client, vendor):
    """文生：chat_type=t2i、files 为空，而且一次 OSS 调用都不该发生。"""
    _QwenVendor.reset("ok")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "a cat", "size": "2K"},
    )
    assert resp.status_code == 200, resp.text
    msg = _msg()
    assert (msg["chat_type"], msg["sub_chat_type"],
            msg["extra"]["meta"]["subChatType"]) == ("t2i", "t2i", "t2i")
    assert msg["files"] == []
    assert msg["extra"]["meta"]["size"] == "auto"      # 档位走 model，size 交给上游
    assert _QwenVendor.sts == [] and _QwenVendor.uploads == []


def test_image_edit_uploads_once_and_switches_every_type_field(client, vendor):
    """图生：getstsToken → PutObject → files[]，三处 chat_type 都变 image_edit。"""
    _QwenVendor.reset("ok")
    resp = client.post(
        "/v1/images/edits",
        headers=_edit_headers(vendor, dict(CREDENTIALS)),
        files={"image": _png("in.png")},
        data={"prompt": "redraw the sky"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["url"] == "https://cdn.qwenlm.ai/fake/1.png"

    assert len(_QwenVendor.sts) == 1 and len(_QwenVendor.uploads) == 1
    sts_req = json.loads(_QwenVendor.sts[0]["body"])
    assert sts_req["filetype"] == "image" and sts_req["filename"].endswith(".png")
    # 上传的是**图片字节**（不是 base64 串），落在 getstsToken 给的那条路径上
    uploaded = _QwenVendor.uploads[0]
    assert uploaded["body"][:8] == b"\x89PNG\r\n\x1a\n"
    assert uploaded["path"].endswith("/webui/upload/1.png")

    msg = _msg()
    assert (msg["chat_type"], msg["sub_chat_type"],
            msg["extra"]["meta"]["subChatType"]) == ("image_edit",) * 3
    assert len(msg["files"]) == 1
    assert msg["files"][0]["type"] == "image"
    assert msg["files"][0]["status"] == "uploaded"
    assert msg["files"][0]["url"] == "https://cdn.example/upload/1.png"
    # 图生链路的 size 无实测依据 ⇒ 两层都不带（顶层 body 与 extra.meta 同一规则）
    assert "size" not in msg["extra"]["meta"]
    assert "size" not in json.loads(_QwenVendor.generations[-1]["body"])
    assert msg["content"] == "redraw the sky"


def test_multi_image_edit_uploads_every_input_in_order(client, vendor):
    """多图：N 张就 N 次上传，且 files[] 顺序与输入顺序一致（不许静默丢图）。"""
    _QwenVendor.reset("ok")
    # 三张图**必须彼此可区分**（尾部各加一个标记）：同字节的三张之下，"第 i 项对应
    # 第 i 张"在断言层面不可观测 —— 这正是本文件早先那条假绿的成因。
    inputs = [PNG_1X1 + b"one", PNG_1X1 + b"two", PNG_1X1 + b"three"]
    resp = client.post(
        "/v1/images/edits",
        headers=_edit_headers(vendor, dict(CREDENTIALS)),
        files=[("image", ("one.png", io.BytesIO(inputs[0]), "image/png")),
               ("image", ("two.png", io.BytesIO(inputs[1]), "image/png")),
               ("image", ("three.png", io.BytesIO(inputs[2]), "image/png"))],
        data={"prompt": "blend these"},
    )
    assert resp.status_code == 200, resp.text
    assert len(_QwenVendor.uploads) == 3, "每张输入图都必须上传一次"

    msg = _msg()
    files = msg["files"]
    assert len(files) == 3
    # 并发物化 ⇒ 断"到达顺序"没有意义（2026-09-19）；断 `files[i]` ↔ refs[i] 的字节，
    # 才是这条用例真正要保的东西。顺带覆盖「N 张就是 N 次上传、不许静默丢图」。
    by_path = _uploads_by_path()
    assert [by_path[f["url"].rsplit("/", 1)[-1]] for f in files] == inputs
    assert [f["type"] for f in files] == ["image"] * 3
    assert msg["content"] == "blend these"
    assert sorted(u["body"] for u in _QwenVendor.uploads) == sorted(inputs)


# --------------------------------------- 生成门 + data URI（真实出图工具走的路）
#
# 上面两条图生图用例走 `/v1/images/edits`（multipart 传文件）。真实出图工具
# `tools/qwen_guest_smoke.py` 走的是**另一条组合**：`/v1/images/generations`
# 外加 `data:` URI 的 JSON 体 —— 它的输入图是自己从产物下载回来的字节，
# 不是用户上传的文件。这条路此前没有任何测试走过（"四个入口都是前门"是路由层
# 的声明，但 data URI 形态的 image 在这一门上具体怎么走，没人验过），
# 而它恰好是每次真实出图都必经的那一跳。以下两条把它钉住。


def _data_uri(payload: bytes = PNG_1X1) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode()


def test_generation_door_takes_a_data_uri_as_an_input_picture(client, vendor):
    """生成门 + 单个 data URI：进 image_edit 链路，且上传的是**解码后的字节**。"""
    _QwenVendor.reset("ok")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "make it snowy", "size": "1K",
              "image": _data_uri()},
    )
    assert resp.status_code == 200, resp.text
    assert len(_QwenVendor.sts) == 1 and len(_QwenVendor.uploads) == 1
    # 上传的必须是图片字节本身：把 base64 文本原样 PUT 上去也会 200，
    # 但上游收到的是坏图 —— 这正是一种"成功"的静默错误。
    assert _QwenVendor.uploads[0]["body"] == PNG_1X1
    assert _QwenVendor.uploads[0]["path"].endswith("/webui/upload/1.png")

    msg = _msg()
    assert (msg["chat_type"], msg["sub_chat_type"],
            msg["extra"]["meta"]["subChatType"]) == ("image_edit",) * 3
    assert len(msg["files"]) == 1 and msg["files"][0]["type"] == "image"
    # 图生链路无尺寸语义：两层都不带（与 edits 门同一条规则）
    assert "size" not in msg["extra"]["meta"]
    assert "size" not in json.loads(_QwenVendor.generations[-1]["body"])


def test_generation_door_multi_image_keeps_both_inputs_in_order(client, vendor):
    """生成门 + 两个 data URI：两张都上传，且顺序与输入顺序一致。"""
    _QwenVendor.reset("ok")
    first, second = _data_uri(PNG_1X1), _data_uri(PNG_1X1 + b"second")
    resp = client.post(
        "/v1/images/generations",
        headers=_headers(vendor, dict(CREDENTIALS)),
        json={"model": "qwen-image", "prompt": "blend these", "size": "1K",
              "image": [first, second]},
    )
    assert resp.status_code == 200, resp.text
    assert len(_QwenVendor.sts) == 2 and len(_QwenVendor.uploads) == 2
    # 🔴 2026-09-19 起垫图是**并发**物化的，于是"PUT 的到达顺序"不再是可观测量 ——
    # 它从来不是契约，只是串行时的副产品。顺序仍然要断，但要断在**真正被保证的地方**：
    # `files[i]` 必须回链到第 i 张输入的字节。做法是把 `files[]` 的 url 映回它那次
    # 上传的 body（假上游两条都记着），再按字节比 —— 比原来的到达顺序更强，它同时钉住
    # 「没有换图」。（"到达顺序"从此不该被任何用例断言，见 `_QwenVendor` 的注释。）
    by_path = _uploads_by_path()
    files = _msg()["files"]
    assert len(files) == 2
    assert [by_path[f["url"].rsplit("/", 1)[-1]] for f in files] == [
        PNG_1X1, PNG_1X1 + b"second"]
