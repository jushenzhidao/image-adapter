"""Unit tests for the image_edit (image-to-image) link of qwen/images@v1.

The t2i decisions live in test_qwen_images_script.py; this file pins the ones
that only exist once input pictures are involved, all of which fail silently
if wrong:

  * the upload link (getstsToken -> OSS V4 PUT) actually runs, once per input;
  * the generation call is addressed to the minted chat_id and carries
    chat_type=image_edit (not t2i) in all three places upstream reads it;
  * several inputs stay several inputs, in order -- dropping all but the first
    is what silently loses a user's picture;
  * `size` is not forwarded on this link (no measured meaning), while an
    explicitly configured image_model still is;
  * failures on the upload hop surface as upstream errors rather than a
    prompt-only request that "succeeds" without the picture.

The script is loaded by path rather than by ref: a unit test should fail on the
function it is about, not on script-store resolution.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from adapter.ctxapi.mapping import MappingMixin
from adapter.sandbox import scan_source
from adapter.utils.fanout import fanout as fanout_all

SCRIPT = Path(__file__).resolve().parents[2] / "script_store" / "qwen" / "images@v1.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("qwen_images_v1_edit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


q = _load()

CREDS = {"cookie": "a=1", "bx_ua": "234!x", "bx_umidtoken": "T2gAx",
         "chat_mode": "guest"}

STS_OK = {"success": True, "data": {
    "access_key_id": "AKID", "access_key_secret": "SECRET",
    "security_token": "STSTOKEN", "bucketname": "qwen-webui-prod",
    "region": "oss-ap-southeast-1", "endpoint": "oss-accelerate.aliyuncs.com",
    "file_id": "fid-1", "file_path": "webui/u1/input.png",
    "file_url": "https://qwen-webui-prod.oss-accelerate.aliyuncs.com/webui/u1/input.png",
}}
CHAT_OK = {"success": True, "data": {"id": "chat-edit-1"}}
IMG_A = "data:image/png;base64,QUFB"
IMG_B = "data:image/png;base64,QkJC"


class FakeResp:
    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text


class _CM:
    """Async context manager with optional in-flight hooks.

    The hooks exist so a test can observe *how many* calls overlap, which is
    the only way to tell a serialised hop from a concurrent one: the call
    order looks exactly the same either way.
    """

    def __init__(self, resp, on_enter=None, on_exit=None):
        self.resp = resp
        self._on_enter = on_enter
        self._on_exit = on_exit

    async def __aenter__(self):
        if self._on_enter is not None:
            self._on_enter()
        # A real yield: without one, `gather`ed tasks never interleave and every
        # concurrency measurement reads 1.
        await asyncio.sleep(0)
        return self.resp

    async def __aexit__(self, *exc):
        if self._on_exit is not None:
            self._on_exit()
        return False


class FakeHttp:
    """Records both hops; getstsToken and chats/new are told apart by URL."""

    def __init__(self, sts=STS_OK, chat=CHAT_OK, put_status=200, put_text=""):
        self.sts = sts
        self.chat = chat
        self.put_status = put_status
        self.put_text = put_text
        self.posts: list[dict] = []
        self.puts: list[dict] = []
        self.sts_calls = 0
        # In-flight high-water marks. `sts_max == 1` is the serialisation
        # contract (a rotating exit must not be used twice at once); `put_max`
        # is what proves the PUTs did *not* get serialised along with it.
        self._sts_live = 0
        self.sts_max = 0
        self._put_live = 0
        self.put_max = 0

    def _sts_enter(self):
        self._sts_live += 1
        self.sts_max = max(self.sts_max, self._sts_live)

    def _sts_exit(self):
        self._sts_live -= 1

    def _put_enter(self):
        self._put_live += 1
        self.put_max = max(self.put_max, self._put_live)

    def _put_exit(self):
        self._put_live -= 1

    def post(self, url, json=None, headers=None):  # noqa: A002 - aiohttp 形状
        self.posts.append({"url": url, "json": json, "headers": headers})
        if "getstsToken" in url:
            # 每次回一份**不同**的 file_path/file_url。全都回同一份的话，多图的
            # "顺序"在断言层面根本不可观测 —— 这条曾经的假绿就是这么来的。
            self.sts_calls += 1
            doc = json_module.loads(json_module.dumps(self.sts))
            data = doc["data"]
            base = "https://qwen-webui-prod.oss-accelerate.aliyuncs.com/webui/u1"
            data["file_path"] = "webui/u1/input" + str(self.sts_calls) + ".png"
            data["file_url"] = base + "/input" + str(self.sts_calls) + ".png"
            data["file_id"] = "fid-" + str(self.sts_calls)
            return _CM(FakeResp(200, json_module.dumps(doc)),
                       self._sts_enter, self._sts_exit)
        return _CM(FakeResp(200, json_module.dumps(self.chat)))

    def put(self, url, data=None, headers=None):
        self.puts.append({"url": url, "data": data, "headers": headers})
        return _CM(FakeResp(self.put_status, self.put_text),
                   self._put_enter, self._put_exit)


json_module = json  # aliased so FakeHttp.post reads naturally above


class FakeCtx(MappingMixin):
    """ctx 站位。继承真的 MappingMixin（框架件，桩里不留副本）。"""

    def __init__(self, options=None, http=None, raw=None, image_bytes=None):
        self.options = options or dict(CREDS)
        self.http = http or FakeHttp()
        self.upstream_raw = raw
        self.upstream_error = None   # 引擎 4xx 重试前写入
        self.upstream_url = "https://chat.qwen.ai/api/v2/chat/completions"
        self.emitted: list[dict] = []
        self.failed = None
        self.request_id = "req-1"
        # 渠道密钥（生产里是 Authorization: Bearer 剥前缀后的那段）。空 = 未提供，
        # 此时身份由 cookie 决定，与真实 ctx（ContextCore.key）同形。
        self.key = ""
        # 显式给定时优先（超限等用例）；默认**由 ref 派生**，这样"第 i 张上传的是
        # 第 i 个输入"才是可证的 —— 全部返回同一份字节时，多图顺序不可观测。
        self._bytes = image_bytes

    @staticmethod
    def bytes_for(ref):
        return b"\x89PNG\r\n\x1a\n" + ref.encode("utf-8")

    async def image_bytes(self, ref):  # noqa: D401 - ctx 契约
        return self._bytes if self._bytes is not None else self.bytes_for(ref)

    def sniff_mime(self, data):
        return "image/png"

    def emit(self, **kw):
        self.emitted.append(kw)

    def fail(self, message, **kw):
        self.failed = (message, kw)
        raise AssertionError(message)

    async def fanout(self, items, work):
        """`ctx.fanout` 的一元门面：真原语，限 5（并发度不是这个脚本的语义）。

        用真件而不是"顺序跑一遍"的替身：扇出的三条契约（保序 / 限量 / 最早失败原样抛）
        都是这条链的一部分，替身会把它们一起假掉。
        """
        return await fanout_all(work, list(items), limit=5)


def _run(ctx, payload):
    return asyncio.run(q.transform(ctx, payload, "request"))


def test_scan_source_clean():
    scan_source(SOURCE, filename="qwen/images@v1.py")


# ------------------------------------------------------------ 分流：t2i / edit

def test_no_image_stays_t2i_and_never_uploads():
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "a cat", "size": "2K"})
    assert body["messages"][0]["chat_type"] == "t2i"
    assert body["messages"][0]["files"] == []
    assert ctx.http.puts == []
    assert [p["url"].rsplit("/", 1)[-1] for p in ctx.http.posts] == ["new"]


def test_single_image_takes_the_edit_link():
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "make it snowy", "image": IMG_A})
    msg = body["messages"][0]
    assert msg["chat_type"] == "image_edit"
    assert msg["sub_chat_type"] == "image_edit"
    assert msg["extra"]["meta"]["subChatType"] == "image_edit"
    # 图生链路没有尺寸语义的实测依据 ⇒ **两层**都不带 size（顶层的与 meta 规则一致）
    assert "size" not in msg["extra"]["meta"]
    assert "size" not in body
    assert len(msg["files"]) == 1
    # 上传链路：一次取凭证、一次 PUT
    assert [p["url"].rsplit("/", 1)[-1] for p in ctx.http.posts] == [
        "getstsToken", "new"]
    assert len(ctx.http.puts) == 1
    assert ctx.http.puts[0]["data"] == ctx.bytes_for(IMG_A)
    # 生成调用被指向新铸的 chat_id
    (emitted,) = ctx.emitted
    assert emitted["query"] == {"chat_id": "chat-edit-1"}


def test_two_images_both_upload_in_order():
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "merge these", "image": [IMG_A, IMG_B]})
    files = body["messages"][0]["files"]
    assert len(files) == 2
    assert len(ctx.http.puts) == 2
    assert [p["url"].rsplit("/", 1)[-1] for p in ctx.http.posts] == [
        "getstsToken", "getstsToken", "new"]
    # 保序必须是**可证的**：只数次数的话，把输入顺序反过来这条照样绿（实测过）。
    assert [p["data"] for p in ctx.http.puts] == [
        ctx.bytes_for(IMG_A), ctx.bytes_for(IMG_B)]
    # files[] 第 i 项回链到第 i 次 getstsToken
    assert [f["url"].rsplit("/", 1)[-1] for f in files] == [
        "input1.png", "input2.png"]
    assert [f["id"] for f in files] == ["fid-1", "fid-2"]


def test_getsts_token_is_serial_while_the_puts_stay_concurrent():
    """The one hop that goes through a rotating exit must not overlap itself.

    2026-09-19: the materialisation is three stages, not one fanout, because
    `getstsToken` is the only call of the three that the channel's proxy
    carries (the PUTs are on the bypass list). Run two of them at once and a
    single client request leaves from two addresses, which is exactly what a
    per-request rotation is supposed to rule out.

    Order alone cannot see this: concurrent and serial calls are recorded in
    the same sequence. The in-flight high-water mark can.
    """
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "merge these", "image": [IMG_A, IMG_B, IMG_B]})

    assert len(body["messages"][0]["files"]) == 3
    assert ctx.http.sts_max == 1, "getstsToken must not overlap itself"
    assert ctx.http.put_max > 1, "the PUTs are direct and should still fan out"
    # Serialising the middle stage must not drop or reorder anything.
    assert [f["id"] for f in body["messages"][0]["files"]] == [
        "fid-1", "fid-2", "fid-3"]


def test_image_mode_first_uploads_only_the_first():
    ctx = FakeCtx(options={**CREDS, "image_mode": "first"})
    body = _run(ctx, {"prompt": "merge", "image": [IMG_A, IMG_B]})
    assert len(body["messages"][0]["files"]) == 1
    assert len(ctx.http.puts) == 1


def test_empty_image_list_is_text_to_image():
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "x", "image": []})
    assert body["messages"][0]["chat_type"] == "t2i"
    assert ctx.http.puts == []


# --------------------------------------------------------------- 字段取舍

def test_edit_drops_size_but_keeps_explicit_image_model():
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-2.0-pro"})
    body = _run(ctx, {"prompt": "x", "image": IMG_A, "size": "2K"})
    meta = body["messages"][0]["extra"]["meta"]
    assert meta["model"] == "qwen-image-2.0-pro"
    # size 在 image_edit 上没有实测语义 ⇒ 不发（不是照 t2i 硬套）
    assert "size" not in meta


def test_t2i_still_sends_size():
    ctx = FakeCtx()
    body = _run(ctx, {"prompt": "x", "size": "2K"})
    assert body["messages"][0]["extra"]["meta"]["size"] == "auto"


# ------------------------------------------------------------- 上传链路零件

def test_v4_headers_shape():
    """形状以 2026-09-18 的 OSS 403 回显为标准答案（见脚本 _v4_headers 注释）：

    region 剥 oss- 前缀；x-oss-content-sha256 只收 UNSIGNED-PAYLOAD；
    canonical headers 不含 host/content-length。
    """
    sts = STS_OK["data"]
    host, region, scheme = q._oss_target(sts)
    assert host == "qwen-webui-prod.oss-accelerate.aliyuncs.com"
    assert region == "ap-southeast-1" and scheme == "https"
    h = q._v4_headers(sts, host, "/" + sts["bucketname"] + "/" + sts["file_path"],
                      region, b"hello", "image/png")
    assert h["Authorization"].startswith("OSS4-HMAC-SHA256 Credential=AKID/")
    assert "Signature=" in h["Authorization"]
    assert h["x-oss-security-token"] == "STSTOKEN"
    assert h["content-type"] == "image/png"
    # 全球加速 endpoint 实测只收 UNSIGNED-PAYLOAD（实算摘要被 400 拒）
    assert h["x-oss-content-sha256"] == "UNSIGNED-PAYLOAD"
    assert "content-length" not in h  # UNSIGNED 模式下不参与 canonical


def test_oss_target_handles_scheme_and_prefixed_endpoint():
    sts = dict(STS_OK["data"], endpoint="http://qwen-webui-prod.oss-cn.aliyuncs.com")
    host, region, scheme = q._oss_target(sts)
    assert host == "qwen-webui-prod.oss-cn.aliyuncs.com"
    assert scheme == "http"


def test_build_file_item_shape():
    item = q._build_file_item(STS_OK["data"], "input.png", "image/png", 42)
    assert item["type"] == "image" and item["showType"] == "image"
    assert item["status"] == "uploaded" and item["progress"] == 100
    assert item["id"] == "fid-1" and item["url"].startswith("https://")
    assert item["file"] == {} and item["size"] == 42
    assert item["file_type"] == "image/png" and item["file_class"] == "image"


def test_filetype_and_ext_mapping():
    assert q._filetype_of("image/webp") == "image"
    assert q._filetype_of("application/pdf") == "file"
    assert q._ext_of("image/jpeg") == "jpg"
    assert q._ext_of("application/octet-stream") == "bin"
    # OSS canonical URI 必须以 "/" 开头 ⇒ 缺前导斜杠时补上（移植自 biz-api 已验证实现）
    assert q._encode_path("webui/u 1/a.png") == "/webui/u%201/a.png"
    assert q._encode_path("/webui/a.png") == "/webui/a.png"


# ---------------------------------------------------------------- 失败通道

def test_upload_http_failure_is_upstream_error():
    ctx = FakeCtx(http=FakeHttp(put_status=403, put_text="SignatureDoesNotMatch"))
    with pytest.raises(AssertionError):
        _run(ctx, {"prompt": "x", "image": IMG_A})
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "OSS upload HTTP 403" in ctx.failed[0]


def test_sts_missing_field_is_upstream_error():
    broken = {"success": True, "data": {"access_key_id": "AKID"}}
    ctx = FakeCtx(http=FakeHttp(sts=broken))
    with pytest.raises(AssertionError):
        _run(ctx, {"prompt": "x", "image": IMG_A})
    assert "missing field" in ctx.failed[0]
    assert ctx.http.puts == []


def test_oversize_input_fails_loudly_instead_of_truncating():
    ctx = FakeCtx(image_bytes=b"x" * (q.SIMPLE_PUT_LIMIT + 1))
    with pytest.raises(AssertionError):
        _run(ctx, {"prompt": "x", "image": IMG_A})
    assert "single-PUT limit" in ctx.failed[0]
    assert ctx.http.puts == []


class _CountedCM:
    """一条请求的进出计数：`peak` 就是"同时在飞的请求数"。

    🔴 里面那句 `await asyncio.sleep` 不是装饰：假 HTTP 若瞬间返回，`asyncio` 只在
    真正挂起的 await 处才切任务 ⇒ 扇出会**看起来是串行的**，`peak` 恒为 1，
    并发用例照样绿（本仓踩过：图片源必须是 `ThreadingHTTPServer`，见
    `tests/integration/test_fanout_materialisation.py`）。
    """

    def __init__(self, resp, http, delay):
        self.resp, self.http, self.delay = resp, http, delay

    async def __aenter__(self):
        self.http.inflight += 1
        self.http.peak = max(self.http.peak, self.http.inflight)
        await asyncio.sleep(self.delay)
        return self.resp

    async def __aexit__(self, *exc):
        self.http.inflight -= 1
        return False


class SlowHttp(FakeHttp):
    """FakeHttp ＋ 真挂起点 ＋ 在飞峰值统计。"""

    def __init__(self, delay=0.01, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.inflight = 0
        self.peak = 0

    def _wrap(self, resp):
        return _CountedCM(resp, self, self.delay)

    def post(self, url, json=None, headers=None):  # noqa: A002 - aiohttp 形状
        cm = super().post(url, json=json, headers=headers)
        return self._wrap(cm.resp)

    def put(self, url, data=None, headers=None):
        cm = super().put(url, data=data, headers=headers)
        return self._wrap(cm.resp)


def test_two_references_upload_concurrently():
    """两张垫图**同时在飞**（2026-09-19 起）：N 张的等待从 N 份压成一份。"""
    ctx = FakeCtx(http=SlowHttp())
    body = _run(ctx, {"prompt": "merge these", "image": [IMG_A, IMG_B]})
    assert len(body["messages"][0]["files"]) == 2
    assert ctx.http.peak == 2, "串行 for 会让峰值恒为 1 —— 这条是用来抓回退的"


def test_a_single_reference_is_left_serial():
    """单张垫图（普通 i2i）走串行路径：扇出对 1 个元素就是退化情形，行为不变。"""
    ctx = FakeCtx(http=SlowHttp())
    _run(ctx, {"prompt": "edit", "image": IMG_A})
    assert ctx.http.peak == 1


def test_concurrent_upload_still_reports_the_first_failure_as_itself():
    """并发不许把「超 2MB 的明确拒绝」塌成通用 502：扇出抛的是**失败项自己的**异常。

    （「最早那一项胜出」由 `adapter/utils/fanout.py` 自己的用例钉；这里钉的是
    **本脚本这条链上**那条明确拒绝没被并发吃掉。）
    """
    ctx = FakeCtx(http=SlowHttp(), image_bytes=b"x" * (q.SIMPLE_PUT_LIMIT + 1))
    with pytest.raises(AssertionError):
        _run(ctx, {"prompt": "merge", "image": [IMG_A, IMG_B]})
    assert "single-PUT limit" in ctx.failed[0]
    assert ctx.failed[1].get("code") == "upstream_error"
    assert ctx.http.puts == []          # 超限在 STS 之前就拒了，一个字节都没上传


# ------------------------------------------------- 上传跳的限流不能被判成"额度耗尽"

def test_upload_rate_limited_with_quota_wording_is_transient_not_429():
    """`getstsToken` 的 `RateLimited` **带额度措辞**时，仍必须是瞬态 502。

    为什么这条要单独钉：上传那一步**不计费**（厂商只对生成调用计费），
    所以它上面出现的任何"额度"措辞都不可能是「生图额度耗尽」——它就是这个过载 code
    的另一副面孔（实测 2026-09-19：按**身份**计窗，账号 20 次/窗口、访客 5 次/窗口）。
    判反的代价不对称：多打一次很快失败的请求 vs **白损失一条身份一整天**。

    对照（守卫）：同一份文档打在**生成跳**上仍然是 429，见
    `test_qwen_images_script.py::test_quota_wording_still_wins_under_an_unknown_code`
    与 `::test_response_quota_maps_to_429_quota_error`。
    """
    refusal = {"success": False,
               "data": {"code": "RateLimited",
                        "details": "今日生图额度已用完，登录后可继续生图。"}}
    ctx = FakeCtx(http=FakeHttp(sts=refusal))
    with pytest.raises(AssertionError):
        _run(ctx, {"prompt": "edit", "image": IMG_A})
    assert ctx.failed[1]["code"] == "upstream_error", "上传跳不许给 429"
    assert ctx.failed[1]["status"] == 502
    assert "不计费" in ctx.failed[0] and "与生图额度无关" in ctx.failed[0]
    assert "生图额度已耗尽" not in ctx.failed[0]


def test_upload_per_call_identity_is_unchanged_by_the_hop_fix():
    """守卫：这次改动只影响**错误分类**，不影响成功路径（每图一次 getstsToken + 一次 PUT）。"""
    ctx = FakeCtx()
    _run(ctx, {"prompt": "edit", "image": IMG_A})
    assert ctx.http.sts_calls == 1
    assert len(ctx.http.puts) == 1
