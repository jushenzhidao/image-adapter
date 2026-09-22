"""Unit tests for qwen/images@v1.

The end-to-end suite proves the wire; this file pins the decisions that break
silently: the fingerprint header set (its completeness is what separates a
drawing from a WAF challenge page), the two-step chat link (one free
chats/new, then the generation URL carrying the fresh chat_id), the SSE
response read from ctx.upstream_raw, and the quota/error classification.

The script is loaded by path rather than by ref: a unit test should fail on
the function it is about, not on script-store resolution.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from adapter.ctxapi.codec import CodecMixin
from adapter.ctxapi.mapping import MappingMixin
from adapter.ctxapi.moderation import ModerationMixin
from adapter.errors import UpstreamError
from adapter.sandbox import scan_source
from adapter.utils.fanout import fanout as fanout_all

SCRIPT = Path(__file__).resolve().parents[2] / "script_store" / "qwen" / "images@v1.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")


def _load():
    spec = importlib.util.spec_from_file_location("qwen_images_v1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


q = _load()


@pytest.fixture(autouse=True)
def _fresh_signin_cache():
    """signin 缓存是**模块级**的，而 TTL 是 6 天、冷却窗是 5 分钟 —— 都远长于一次
    测试 ⇒ 不清空就会有顺序依赖（前一个用例登进去，后一个用例就不再登了）。

    结构是**按账号**分格（`{cache_key: {jwt, ts, inflight}}`），所以清空整个 dict。
    "被上游挡着"（`_BLOCKED`：x5sec 突发 120 s / WAF 挑战页 600 s）同理，
    都是模块级状态，不清就会串到别的用例。"""
    q._SIGNIN_STATE.clear()
    q._BLOCKED.clear()
    q._REQUESTS.clear()
    yield
    q._SIGNIN_STATE.clear()
    q._BLOCKED.clear()
    q._REQUESTS.clear()


CREDS = {"cookie": "a=1", "bx_ua": "234!x", "bx_umidtoken": "T2gAx",
         "chat_mode": "guest"}

SSE_OK = (
    'data: {"choices":[{"delta":{"content":'
    '"https://cdn.qwenlm.ai/output/x/1.png"}}]}\n'
    'data: {"choices":[{"delta":{"content":"done"}}]}\n'
)
QUOTA_JSON = {"success": False,
              "data": {"code": "RateLimited",
                       "details": "今日生图额度已用完，登录后可继续生图。"}}
CHAT_OK = {"success": True, "data": {"id": "chat-1"}}


class FakeHeaders(dict):
    """响应头。`Set-Cookie` 可以有多条 ⇒ aiohttp 用 getall，普通 mapping 只有 get。"""

    def getall(self, key, default=None):
        value = self.get(key)
        if value is None:
            return [] if default is None else default
        return list(value) if isinstance(value, (list, tuple)) else [value]


class FakeResp:
    def __init__(self, status=200, text="", headers=None, delay=0.0):
        self.status = status
        self._text = text
        self.headers = FakeHeaders(headers or {})
        # delay 用来让并发用例真的能交错：没有挂起点就测不到单飞。
        self.delay = delay

    async def text(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._text


class FakeCache:
    """ctx.cache 站位（L2）。面与 `redis.asyncio` / `TTLCache` 一致：字节值 + ex。"""

    def __init__(self):
        self.store = {}
        self.gets = []
        self.sets = []

    async def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    async def set(self, key, value, ex=300):  # noqa: A002 - 库的形状
        self.sets.append((key, value, ex))
        self.store[key] = value


class BrokenCache:
    """缓存不可用（比如 Redis 挂了）：读写的失败都不该影响请求本身。"""

    async def get(self, key):
        raise RuntimeError("redis down")

    async def set(self, key, value, ex=300):  # noqa: A002 - 库的形状
        raise RuntimeError("redis down")


def _signin_ok(token, body=None):
    """登录成功的真实形状：token 在 Set-Cookie，body 是账号记录。"""
    return FakeResp(200,
                    body if body is not None
                    else json.dumps({"success": True, "data": {"id": "u1"}}),
                    headers={"Set-Cookie": "token=" + token + "; Path=/; HttpOnly"})


class FakeHttp:
    """ctx.http 站位：记录调用，按 URL 子串路由回放响应。"""

    def __init__(self, resp, routes=None):
        self.resp = resp
        self.routes = routes or {}   # url 子串 → FakeResp
        self.calls = []

    class _CM:
        def __init__(self, resp):
            self.resp = resp

        async def __aenter__(self):
            return self.resp

        async def __aexit__(self, *exc):
            return False

    def _pick(self, url):
        for sub, resp in self.routes.items():
            if sub in url:
                return resp
        return self.resp

    def post(self, url, json=None, headers=None):  # noqa: A002 - aiohttp 形状
        self.calls.append({"url": url, "json": json, "headers": headers})
        return FakeHttp._CM(self._pick(url))

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return FakeHttp._CM(self._pick(url))


class FakeLogfire:
    """ctx.logfire 站位。脚本对 trace 的写入是"尽力而为"的，所以只记录、不断言调用次数。"""

    def __init__(self):
        self.notes = []

    def info(self, message, **attrs):
        self.notes.append({"message": message, **attrs})


class _FakeSettings:
    """`ctx.settings` 站位：只提供 `ModerationMixin` 要读的 `capability_roots`。

    留空元组＝"没有能力表"，于是共享词表走 mixin 自带的**实测默认**——正是数据文件缺失时
    生产里的行为，单元测试不该依赖仓库里那份 JSON 的内容。
    """

    capability_roots: tuple = ()


class FakeCtx(MappingMixin, CodecMixin, ModerationMixin):
    """ctx 站位。继承**真的** MappingMixin：`ctx.parse_size` 是框架件，桩里不该有副本，
    否则尺寸方言的回归会在框架与桩各测一遍、而且两边可以漂移。

    `CodecMixin` 同为真件（`encode_b64` / `sniff_mime` 是纯函数）。
    `ModerationMixin` 也是真件：审核判定与出口契约**只在框架里有一份**（判定用哪张词表、
    出口给什么状态码都是契约），桩里再抄一份就等于把契约测成两份、还能各自漂移。下载与外呼 fan-out
    则是**替身**：这里钉的是脚本怎么用它们的返回值，不是框架的判据
    （`download_image` 的 SSRF / 体积 / magic bytes 在 `tests/unit/test_image_ref.py`）；
    并发度不是脚本的语义，故 fan-out 走真原语、限 5。
    """

    def __init__(self, options=None, http=None, raw=None, logfire=True, key="",
                 cache=None, blobs=None, stored_link=None, rehost_error=None):
        # `is not None` 而不是 `or`：空 dict 是"没有配置"这个合法取值，
        # 用 or 会让它悄悄退回 CREDS（"凭据缺失"那条用例就再也测不到）。
        self.options = dict(CREDS) if options is None else options
        self.http = http or FakeHttp(FakeResp(200, json.dumps(CHAT_OK)))
        self.cache = FakeCache() if cache is None else cache
        self.upstream_raw = raw
        self.upstream_error = None   # 引擎在 4xx 重试前写入；脚本据此作废缓存的 JWT
        self.upstream_url = "https://chat.qwen.ai/api/v2/chat/completions"
        self.settings = _FakeSettings()
        self.emitted = []
        self.failed = None
        self.request_id = "req-1"
        # 渠道密钥 = 生产里 Authorization: Bearer 剥前缀后的那段（ContextCore.key）。
        self.key = key
        # logfire=False 用来证明"没有这个属性时脚本照样跑完"——它在生产里是
        # 可选的仪表盘，不是契约。
        if logfire:
            self.logfire = FakeLogfire()
        # 产物下载（`response_format: b64_json`，以及死链判据都走它）。**没有登记
        # 字节的 URL 一律按"取回的不是图"报错** —— 那正是参考仓实测到的死链形状
        # （404 的 226 字节 HTML 页），不是随便挑的失败。
        self.blobs = dict(blobs or {})
        self.downloads = []
        # `ctx.rehost_image` 站位（机制判据在 test_image_ref.py 用真件测）：
        # `stored_link` 是转存产物；None = 无存储 ⇒ 机制按契约回 None，
        # 脚本应透传上游链接；`rehost_error` 模拟死链的响亮失败。
        self.stored_link = stored_link
        self.rehost_error = rehost_error
        self.rehosts = []

    def emit(self, **kw):
        self.emitted.append(kw)

    def fail(self, message, **kw):
        self.failed = (message, kw)
        raise AssertionError(message)

    async def download_image(self, url):
        """`ctx.download_image` 站位：登记过的 URL 回字节，其余按死链报错。

        真件的拒绝码是 `image_content_type`（400），脚本不该改写它 —— 谁要的输出
        形态谁付这次下载，而这次下载顺带就是真伪判据。
        """
        self.downloads.append(url)
        raw = self.blobs.get(url)
        if raw is None:
            raise UpstreamError(
                "Expected an image, got Content-Type 'text/html'",
                code="image_content_type", status=400,
            )
        return raw

    async def rehost_image(self, url):
        """`ctx.rehost_image` 站位：登记调用、按配置回转存产物或失败。"""
        self.rehosts.append(url)
        if self.rehost_error is not None:
            raise self.rehost_error
        return self.stored_link

    async def fanout(self, items, work):
        """`ctx.fanout` 的一元门面：真原语，限 5（并发度不是这个脚本的语义）。"""
        return await fanout_all(work, list(items), limit=5)


def test_scan_source_clean():
    scan_source(SOURCE, filename="qwen/images@v1.py")


# ---------------------------------------------------------------- auth 相位

def test_auth_phase_emits_fingerprint_headers():
    ctx = FakeCtx()
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Cookie"] == "a=1" and h["bx-ua"] == "234!x"
    assert h["bx-umidtoken"] == "T2gAx" and h["version"] == "0.2.0"
    assert h["Sec-Fetch-Mode"] == "cors" and "sec-ch-ua" in h
    assert h["source"] == "web"
    # 抑制引擎的凭证发射（web 端点拒绝任何 Authorization；实测 09-22 该头一票
    # 否决）——空值＝不发（`transport.build_request` 丢弃空值头）。
    assert h["Authorization"] == ""


def test_request_phase_re_emits_the_full_header_set():
    """重试路径：引擎在 4xx 重试前 `reset_plan()`，auth 相位 emit 的头会被清空
    —— request 相位必须重发全套（含 Authorization 抑制与浏览器指纹），否则第二
    发裸奔、连 WAF 都过不去。"""
    ctx = FakeCtx()
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "auth"))
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))

    emitted = [e["headers"] for e in ctx.emitted if "headers" in e]
    assert any(h.get("Authorization") == "" for h in emitted), emitted
    assert any("User-Agent" in h and "Sec-Fetch-Mode" in h for h in emitted)


def test_auth_phase_missing_credentials_is_channel_error():
    ctx = FakeCtx(options={"cookie": "a=1"})
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {}, "auth"))
    assert ctx.failed[1]["code"] == "channel_config_error"


# ------------------------------------------------------------- request 相位

def test_request_phase_creates_chat_then_emits_generation_url():
    ctx = FakeCtx()
    body = asyncio.run(q.transform(ctx, {"prompt": "a cat", "size": "2K"},
                                   "request"))
    # 恰好一次 chats/new：免费、无内容，不触碰「一次生成调用」不变量
    assert len(ctx.http.calls) == 1
    assert ctx.http.calls[0]["json"]["chat_mode"] == "guest"
    (emitted,) = [e for e in ctx.emitted if "url" in e]
    assert emitted["url"].endswith("/v2/chat/completions")
    assert emitted["query"] == {"chat_id": "chat-1"}
    assert body["chat_id"] == "chat-1" and body["chat_mode"] == "guest"
    assert body["messages"][0]["chat_type"] == "t2i"
    assert body["messages"][0]["content"] == "a cat"
    # "2K" 走档位映射（CREDS 未带 image_model，model 字段来自档位表）
    assert body["messages"][0]["extra"]["meta"]["model"] == "qwen-image-3.0-pro"


def test_size_mapping_rules():
    ctx = FakeCtx()
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "1024x1024"},
                                   "request"))
    assert body["size"] == "1024*1024"
    assert "model" not in body["messages"][0]["extra"]["meta"]
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "16:9"},
                                   "request"))
    assert body["size"] == "16:9"
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "not-a-size"},
                                   "request"))
    # 无法识别 → auto（比例由模型按提示词决定），绝不 400
    assert body["size"] == "auto"


def test_image_model_option_wins_over_tier(tmp_path=None):
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-3.0"})
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "2K"},
                                   "request"))
    assert body["messages"][0]["extra"]["meta"]["model"] == "qwen-image-3.0"


#: 实测（2026-09-18 反向代理）：`-pro` 与显式像素的组合会挂满 300s 再 internal_error，
#: 非 pro 模型则是静默忽略 ⇒ 护栏只该落在 pro 上，且只该落在"非表内像素对"上。


def test_pro_model_drops_an_explicit_pixel_pair_to_auto():
    """像素请求在 pro 上必然失败 ⇒ 改发能跑通的 `auto`，并把丢弃写进 trace。"""
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-3.0-pro"})
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "3840x2160"},
                                   "request"))
    assert body["size"] == "auto"
    (note,) = [n for n in ctx.logfire.notes if n["stage"] == "size_guard"]
    assert note["requested"] == "3840x2160" and note["sent"] == "auto"
    assert note["size_hw"] == "3840*2160"


def test_pro_model_still_takes_the_ratio_enum():
    """挂死的是**非表内像素对**，比例枚举不在其中 —— 别顺手把比例也改成 auto。"""
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-3.0-pro"})
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "16:9"},
                                   "request"))
    assert body["size"] == "16:9"
    assert not [n for n in ctx.logfire.notes if n["stage"] == "size_guard"]


def test_pro_model_still_accepts_a_canonical_resolution_as_its_ratio():
    """表内分辨率会被换算成比例枚举（前端方言），那条路同样不受影响。"""
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-3.0-pro"})
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "2688*1536"},
                                   "request"))
    assert body["size"] == "16:9"


def test_non_pro_model_keeps_the_pixels_verbatim():
    """非 pro 模型静默忽略像素（实测），没有挂死风险 ⇒ 行为不变。"""
    ctx = FakeCtx(options={**CREDS, "image_model": "qwen-image-3.0"})
    body = asyncio.run(q.transform(ctx, {"prompt": "x", "size": "3840x2160"},
                                   "request"))
    assert body["size"] == "3840*2160"


# ------------------------------------------------------------ response 相位

def test_response_phase_parses_sse_from_raw():
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"))
    out = asyncio.run(q.transform(ctx, None, "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"


def test_response_phase_parses_sse_from_payload_bytes():
    """真实引擎形态：reply.payload 对 SSE 直接就是 raw bytes，而不是 None。"""
    ctx = FakeCtx()
    out = asyncio.run(q.transform(ctx, SSE_OK.encode("utf-8"), "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"


def test_response_quota_maps_to_429_quota_error():
    ctx = FakeCtx()
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, QUOTA_JSON, "response"))
    assert ctx.failed[1]["code"] == "upstream_quota_exhausted"
    assert ctx.failed[1]["status"] == 429
    assert ctx.failed[1]["err_type"] == "rate_limit_error"


def test_response_generic_error_is_upstream_error():
    ctx = FakeCtx()
    payload = {"success": False,
               "data": {"code": "RGV587_ERROR::SM", "details": "validate"}}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["code"] == "upstream_error"


def test_chats_new_waf_page_fails_as_upstream_error():
    """挑战页报的必须是"被 WAF 挑战页拦下 + 等冷却或换出口"，不是泛泛的 non-JSON。"""
    ctx = FakeCtx(http=FakeHttp(FakeResp(
        200, "<!doctype html><meta name=\"aliyun_waf_aa\" content=\"x\">")))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert "WAF 挑战页" in ctx.failed[0] and "换出口" in ctx.failed[0]


# ------------------------------------------------ 用户模式 / 访客模式（同一脚本）

#: 登录态 cookie 的特征是带着 `token` JWT（§2.2）；访客态没有（§2.6 逐键列举过）。
USER_COOKIE = "aui=x; token=eyJhbGciOiJIUzI1NiJ9.payload.sig; cna=y"


def test_mode_is_derived_from_the_credential():
    """模式由凭据自身派生 ⇒ 两种渠道都不用声明，也就不会declare 错。"""
    user = asyncio.run(q.transform(FakeCtx(options={"cookie": USER_COOKIE}),
                                   {}, "auth"))
    assert user["Referer"] == "https://chat.qwen.ai/c/new-chat"
    guest = asyncio.run(q.transform(FakeCtx(), {}, "auth"))
    assert guest["Referer"] == "https://chat.qwen.ai/c/guest"


def test_declared_mode_wins_over_derivation():
    ctx = FakeCtx(options={**CREDS, "cookie": USER_COOKIE, "chat_mode": "guest"})
    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    assert headers["Referer"] == "https://chat.qwen.ai/c/guest"
    body = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert ctx.http.calls[0]["json"]["chat_mode"] == "guest"
    assert body["chat_mode"] == "guest"


def test_mode_carries_into_both_calls_of_the_generation_link():
    ctx = FakeCtx(options={"cookie": USER_COOKIE})
    body = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert ctx.http.calls[0]["json"]["chat_mode"] == "normal"
    assert body["chat_mode"] == "normal"


def test_guest_needs_device_identity_but_logged_in_does_not():
    """§2.6：访客态唯一凭据就是设备指纹。§2.5 variant D：登录态只靠 cookie 也能流。"""
    refused = FakeCtx(options={"cookie": "aui=x"})
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(refused, {}, "auth"))
    assert refused.failed[1]["code"] == "channel_config_error"
    assert "guest mode" in refused.failed[0]

    ok = FakeCtx(options={"cookie": USER_COOKIE})
    headers = asyncio.run(q.transform(ok, {}, "auth"))
    assert headers["Cookie"] == USER_COOKIE
    # 没配就不发，绝不因为缺它们而 KeyError
    assert "bx-ua" not in headers and "bx-umidtoken" not in headers


def test_fingerprint_headers_carry_timezone_and_request_id():
    """两条头在两种模式的抓包里都有，而最早那份头集合漏了它们。"""
    ctx = FakeCtx()
    h1 = asyncio.run(q.transform(ctx, {}, "auth"))
    h2 = asyncio.run(q.transform(ctx, {}, "auth"))
    # "Fri Sep 18 2026 12:13:12 GMT+0800" —— Date().toString() 的形状
    parts = h1["Timezone"].split(" ")
    assert len(parts) == 6 and len(parts[5]) == 8 and parts[5].startswith("GMT")
    assert h1["X-Request-Id"] and h1["X-Request-Id"] != h2["X-Request-Id"]


# --------------------------------------- 渠道密钥（Bearer）当登录态凭据用

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6IjUxN2IifQ.sig"


def _has_token_entry(cookie):
    """cookie 里有没有名为 token 的条目。

    不能用 `"token=" in cookie` 判：`bx-umidtoken=` 里就含这个子串，
    实测这条断言写错过一次 —— 是断言的问题，不是产品的问题。
    """
    return any(p.partition("=")[0].strip() == "token" for p in cookie.split(";"))


def test_channel_key_is_the_account_token_and_lands_in_the_cookie():
    """控制面把 token 放渠道密钥里（`Authorization: Bearer <jwt>`）—— 两条槽位等价。"""
    ctx = FakeCtx(options={"cookie": "aui=x; ssxmod_itna=y"}, key=JWT)
    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    assert ("token=" + JWT) in headers["Cookie"]
    assert headers["Cookie"].startswith("aui=x")        # 其余条目原样保留
    assert headers["Referer"] == "https://chat.qwen.ai/c/new-chat"
    body = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert body["chat_mode"] == "normal"


def test_key_token_replaces_a_stale_one_in_the_jar():
    """jar 里那份可能是旧的；两份并列等于让上游挑，auth 路径不掷硬币。"""
    ctx = FakeCtx(options={"cookie": "token=OLD; aui=x"}, key=JWT)
    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    names = [p.partition("=")[0].strip() for p in headers["Cookie"].split(";")]
    assert names.count("token") == 1
    assert "OLD" not in headers["Cookie"]


def test_key_alone_is_a_complete_logged_in_channel():
    """只有密钥也能跑：§2.5 variant D 用很少的 cookie 就流起来了。"""
    ctx = FakeCtx(options={}, key=JWT)
    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    assert headers["Cookie"] == "token=" + JWT
    body = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert body["chat_mode"] == "normal"
    # 登录态不索要设备指纹（有则发、无则不发）
    assert "bx-ua" not in headers and "bx-umidtoken" not in headers


def test_key_says_guest_explicitly_while_empty_key_defers_to_the_jar():
    """"留空或者 guest"：字面 guest = 明确走访客门；留空 = 不表态，由 jar 决定。"""
    declared = FakeCtx(options={**CREDS, "cookie": "aui=x; bx-umidtoken=z"},
                       key="guest")
    headers = asyncio.run(q.transform(declared, {}, "auth"))
    assert headers["Referer"] == "https://chat.qwen.ai/c/guest"
    assert not _has_token_entry(headers["Cookie"])
    assert headers["Cookie"] == "aui=x; bx-umidtoken=z"   # jar 原样

    # 字面 guest 优先于 jar 里残留的 token，否则"访客"渠道会悄悄变登录态
    stale = FakeCtx(options={"cookie": "token=OLD; aui=x", "bx_ua": "b",
                             "bx_umidtoken": "T"}, key="guest")
    assert not _has_token_entry(
        asyncio.run(q.transform(stale, {}, "auth"))["Cookie"])

    # 留空不作表态：身份仍由 jar 决定（无 token ⇒ 访客）
    empty = FakeCtx(options={"cookie": "aui=x", "bx_ua": "b", "bx_umidtoken": "T"},
                    key="")
    assert asyncio.run(
        q.transform(empty, {}, "auth"))["Referer"].endswith("/c/guest")


def test_key_guest_without_device_identity_is_refused():
    ctx = FakeCtx(options={"cookie": "aui=x"}, key="guest")
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {}, "auth"))
    assert ctx.failed[1]["code"] == "channel_config_error"
    assert "guest mode" in ctx.failed[0]


def test_no_credential_at_all_is_a_channel_config_error():
    """密钥与 cookie 都缺 ⇒ 发上游之前就 400，而不是等一个 WAF 页回来。"""
    ctx = FakeCtx(options={})
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {}, "auth"))
    assert ctx.failed[1]["code"] == "channel_config_error"
    assert "token (channel key) or cookie" in ctx.failed[0]


# ------------------------------------------------------------- size 的三种方言


def test_size_dialects_are_answered_in_the_frontend_dialect():
    """比例 / 分辨率 / 档位都指同一件事，出去的一律是前端真正会发的形式。"""
    expected = {
        "16:9": ("16:9", "2688*1536"),
        # 分辨率若就是某个比例的规范值，就近换算成比例再发
        "2688*1536": ("16:9", "2688*1536"),
        "2688x1536": ("16:9", "2688*1536"),
        # 非规范分辨率原样透传：上游是否接受未验证，如实发，不擅自塑形
        "1024x1024": ("1024*1024", "1024*1024"),
        "3840*2160": ("3840*2160", "3840*2160"),
    }
    for raw, (size, hw) in expected.items():
        assert q._resolve_size(FakeCtx(), {"size": raw}, {}) == (size, None, hw), raw


def test_unrecognized_size_still_falls_back_to_auto():
    assert q._resolve_size(FakeCtx(), {"size": "not-a-size"}, {}) == ("auto", None, None)
    assert q._resolve_size(FakeCtx(), {"size": "auto"}, {}) == ("auto", None, None)


def test_size_hw_is_recorded_on_the_trace():
    ctx = FakeCtx()
    asyncio.run(q.transform(ctx, {"prompt": "x", "size": "16:9"}, "request"))
    (note,) = [n for n in ctx.logfire.notes if n["stage"] == "request"]
    assert note["size"] == "16:9" and note["size_hw"] == "2688*1536"
    assert note["chat_mode"] == "guest" and note["input_images"] == 0


# --------------------------------------------------------- 无图响应的归因与元数据

SSE_META = (
    'data: {"choices":[{"delta":{"content":'
    '"https://cdn.qwenlm.ai/output/x/1.png"}}],'
    '"usage":{"output_width":2688,"output_height":1536,'
    '"output_image_count":1,"output_image_type":"qima_output_2k"}}\n'
    'data: {"choices":[{"delta":{"extra":{"output_image_hw":[[1536,2688]]},'
    '"status":"finished"}}]}\n'
)
QUOTA_FRAME = ('data: {"success":false,"data":{"code":"RateLimited",'
               '"details":"今日生图额度已用完，登录后可继续生图。"}}\n')


def test_metadata_goes_to_the_trace_not_the_body():
    """上游自报的分辨率与张数进 trace；响应体仍是 OpenAI 的 images 形状。"""
    ctx = FakeCtx(raw=SSE_META.encode("utf-8"))
    out = asyncio.run(q.transform(ctx, None, "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"
    assert set(out) == {"created", "data"}
    (note,) = [n for n in ctx.logfire.notes if n["stage"] == "response"]
    assert note["urls"] == 1
    assert note["output_width"] == 2688 and note["output_height"] == 1536
    assert note["output_image_type"] == "qima_output_2k"
    assert note["output_image_count"] == 1
    # 上游没发的键不许被编出来（`width`/`height` 是另一套命名的方言）
    assert "width" not in note and "height" not in note
    # 不替上游裁决 (高,宽) 还是 (宽,高)：原样带出
    assert note["output_image_hw"] == [1536, 2688]


def test_trace_note_is_optional():
    """没有 logfire 的环境（未配 token）必须照样跑完 —— 它不是契约。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), logfire=False)
    out = asyncio.run(q.transform(ctx, None, "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"


def test_quota_arriving_as_a_stream_frame_is_not_read_as_a_missing_url():
    ctx = FakeCtx(raw=QUOTA_FRAME.encode("utf-8"))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["code"] == "upstream_quota_exhausted"
    assert ctx.failed[1]["status"] == 429


#: 3.0-pro 的额度耗尽走**流内 error 帧**，而不是 JSON 拒绝（2026-09-18 反向代理实测）。
PRO_QUOTA_FRAME = ('data: {"error":{"code":"quota_limit",'
                   '"detail":"今日生图额度已用完，登录后可继续生图。"}}\n')
#: 同一个 `quota_limit` 也承载**瞬时过载**；两者只差文案（同批实测）。
OVERLOAD_FRAME = ('data: {"error":{"code":"quota_limit",'
                  '"details":"目前服务访问量较大，请稍后再试。"}}\n')


def test_quota_delivered_as_a_stream_error_frame_maps_to_429():
    """流内 `error` 帧与 JSON 拒绝是**同一件事的两种投递方式**。

    只认 `data.code` 会把它降级成「未返回图片 URL」（无 429、无额度码），
    调用方就会继续复用一条已经耗尽的身份。
    """
    ctx = FakeCtx(raw=PRO_QUOTA_FRAME.encode("utf-8"))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["code"] == "upstream_quota_exhausted"
    assert ctx.failed[1]["status"] == 429
    assert ctx.failed[1]["err_type"] == "rate_limit_error"
    assert "额度已用完" in ctx.failed[0]


def test_overload_under_the_quota_code_is_not_a_spent_identity():
    """`quota_limit` 是**过载的** code：瞬时过载不许被记成「该身份今日耗尽」。

    判反的代价不对称 —— 多打一次很快失败的请求 vs 白损失一条身份的整天额度。
    """
    ctx = FakeCtx(raw=OVERLOAD_FRAME.encode("utf-8"))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert ctx.failed[1]["status"] == 502
    assert "瞬时过载" in ctx.failed[0] and "不是该身份当日额度耗尽" in ctx.failed[0]


def test_rate_limited_without_the_quota_wording_is_transient():
    """JSON 方言同样过载：`RateLimited` + 过载文案 ≠ 日额度。"""
    ctx = FakeCtx()
    payload = {"success": False,
               "data": {"code": "RateLimited",
                        "details": "目前服务访问量较大，请稍后再试。"}}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert ctx.failed[1]["status"] == 502


def test_quota_wording_still_wins_under_an_unknown_code():
    """文案是权威：认得出的日额度文案，即便 code 不认识也该给 429。"""
    ctx = FakeCtx()
    payload = {"success": False,
               "data": {"code": "SomethingNew",
                        "details": "今日生图额度已用完，登录后可继续生图。"}}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["code"] == "upstream_quota_exhausted"
    assert ctx.failed[1]["status"] == 429


def test_x5sec_ret_shape_is_named_as_such():
    """{"ret":[...]} 没有 data 字段，不能落进「没有图数据」那一条。"""
    ctx = FakeCtx()
    payload = {"ret": ["FAIL_SYS_USER_VALIDATE",
                       "RGV587_ERROR::SM::哎哟喂,被挤爆啦"]}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "x5sec" in ctx.failed[0]


def test_diagnose_names_the_four_shapes():
    assert "WAF" in q._diagnose('<!doctype html><meta name="aliyun_waf_aa">')
    assert "x5sec" in q._diagnose('{"ret":["FAIL_SYS_USER_VALIDATE","RGV587"]}')
    assert "空 SSE" in q._diagnose("event: ping\n")      # 0 条 data 行
    assert "无图片 URL" in q._diagnose('data: {"choices":[]}\n')


def test_missing_url_reports_the_shape_not_just_the_absence():
    ctx = FakeCtx(raw=b'data: {"choices":[{"delta":{"content":"no picture"}}]}\n')
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert "有 SSE 但无图片 URL" in ctx.failed[0]


# ------------------------------------------------- 按生产的方式加载（builtins 白名单）

def _load_sandboxed() -> dict:
    """exec + SAFE_BUILTINS，与 adapter.script_cache 装载脚本的方式一致。

    本文件顶部的 import 给脚本的是**完整 builtins**，所以「生产里根本没有的名字」
    （实测 `hasattr` 不在 SAFE_BUILTINS 里，而沙箱 AST 扫描也不拦它）在这里永远绿。
    这一份就是为了堵这条缝：它曾经让三相位脚本在单测全绿的情况下 500。
    """
    from adapter.script_cache import SAFE_BUILTINS

    namespace: dict = {"__builtins__": SAFE_BUILTINS, "__name__": "adapter_script"}
    exec(compile(SOURCE, "<script:sandboxed>", "exec"), namespace)  # noqa: S102
    return namespace


def test_all_phases_run_under_the_real_builtins_whitelist():
    sandboxed = _load_sandboxed()
    transform = sandboxed["transform"]

    # 三相位都要真跑一遍：缺一个名字就够 500，而单测不会告诉你
    headers = asyncio.run(transform(FakeCtx(), {}, "auth"))
    assert headers["version"] == "0.2.0"
    body = asyncio.run(transform(FakeCtx(), {"prompt": "x", "size": "2K"}, "request"))
    assert body["chat_id"] == "chat-1"
    out = asyncio.run(transform(FakeCtx(raw=SSE_OK.encode("utf-8")), None, "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"


def test_trace_note_survives_the_whitelist_without_logfire():
    """没有 logfire 时要靠 try/except 兜住 AttributeError —— 白名单里没有 hasattr。"""
    transform = _load_sandboxed()["transform"]
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), logfire=False)
    out = asyncio.run(transform(ctx, None, "response"))
    assert out["data"][0]["url"] == "https://cdn.qwenlm.ai/output/x/1.png"


FULL_COOKIE_KEY = "a=1; token=eyJlogin; b=2"


def test_bearer_full_cookie_string_is_the_third_form():
    """Authorization: Bearer <整串登录cookie> —— cookie 串就是 jar，token 从中取。"""
    ctx = FakeCtx(options={}, key=FULL_COOKIE_KEY)
    assert q._bearer_token(ctx) == "eyJlogin"
    assert q._jar_for(ctx, "") == FULL_COOKIE_KEY
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Cookie"] == FULL_COOKIE_KEY


def test_bearer_full_cookie_string_beats_options_cookie():
    """key 携带的 jar 与 options.cookie 并存时，key 是操作者的显式决定。"""
    ctx = FakeCtx(options={"cookie": "stale=1"}, key=FULL_COOKIE_KEY)
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Cookie"] == FULL_COOKIE_KEY


def test_request_phase_uses_cookie_string_key():
    ctx = FakeCtx(options={}, key=FULL_COOKIE_KEY)
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    sent = ctx.http.calls[0]["headers"]
    assert sent["Cookie"] == FULL_COOKIE_KEY


def test_pair_form_signs_in_then_uses_jwt():
    """形态④：Bearer <user|pass> → signin 换 JWT（v2 端点、token 在 Set-Cookie）。"""
    http = FakeHttp(_signin_ok("jwt-from-signin"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    h = asyncio.run(q.transform(ctx, {}, "auth"))

    posts = [c for c in http.calls if c.get("json")]
    assert posts, "signin 未发生"
    assert posts[0]["url"].endswith("/v2/auths/signin")      # v2：只有账号档案在 v1
    assert posts[0]["json"]["email"] == "user@mail.test"
    # 无盐 sha256(明文口令)，服务端收的就是哈希（参考实现反查命中）
    assert posts[0]["json"]["password"] == hashlib.sha256(b"passw0rd").hexdigest()
    assert "token=jwt-from-signin" in h["Cookie"]


def test_signin_ignores_a_token_in_the_body():
    """token 只在 Set-Cookie 里；body 是账号记录 —— 从 body 取会静默登不进去。"""
    body_only = FakeResp(200, json.dumps({"success": True, "token": "jwt-in-body"}),
                         headers={"Set-Cookie": "acw_tc=1; Path=/"})
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=FakeHttp(body_only), key="user@mail.test|passw0rd")
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert "jwt-in-body" not in h["Cookie"]
    # 拿不到 token ⇒ 回落 guest 门（而不是把一个空的 token 送上去）
    assert h["Referer"].endswith("/c/guest")


def test_signin_warmup_gets_the_waf_cold_start_cookies_first():
    """预热打的是**源站根**的 `/auth`，不是 API 前缀下的 `/api/auth` —— 后者是 404，
    而 best-effort 的处理器会把它吞掉，于是那个 WAF 冷启动 cookie 从来没拿到过。"""
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))
    urls = [c["url"] for c in http.calls]
    assert urls[0] == "https://chat.qwen.ai/auth", urls[0]
    assert urls[1] == "https://chat.qwen.ai/api/v2/auths/signin"


# --- 会话 jar 捕获（pair 形态的身份自洽，2026-09-22） -----------------------


def test_cookie_header_takes_pairs_not_attributes_and_last_wins():
    """Set-Cookie → Cookie 头：只取第一段，属性丢掉；同名后出现者为准。"""
    out = q._cookie_header(["acw_tc=1; Path=/; HttpOnly",
                            "x-ap=2; Path=/",
                            "acw_tc=9; Domain=.qwen.ai",
                            "junk"])
    assert out == "acw_tc=9; x-ap=2"


def test_pair_form_captures_the_signin_session_jar():
    """pair 形态把「预热 + 登录」自己种下的 cookie 收成 jar，与 token 同源。

    写请求带的是这份**自洽身份**，而不是把 token 拼进配置的 `cookie` 选项
    （运营者的 jar）——实测 2026-09-22 正是那种混搭被 x5sec 拒。
    """
    warm = FakeResp(200, "<html>ok</html>",
                    headers={"Set-Cookie": ["acw_tc=x1; Path=/; HttpOnly",
                                            "x-ap=54; Path=/"]})
    http = FakeHttp(_signin_ok("jwt-new"),
                    routes={"chat.qwen.ai/auth": warm})
    ctx = FakeCtx(options={"cookie": "someone-else=jar; token=oldjwt"},
                  http=http, key="user@mail.test|passw0rd")
    h = asyncio.run(q.transform(ctx, {}, "auth"))

    for part in ("acw_tc=x1", "x-ap=54", "token=jwt-new"):
        assert part in h["Cookie"], h["Cookie"]
    assert "someone-else=jar" not in h["Cookie"], "不许混入别人的会话"
    assert "oldjwt" not in h["Cookie"]
    # 同一份 jar 也要上到 request 相位的写请求上
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    sent = [c for c in http.calls if c.get("json")][-1]["headers"]
    assert sent["Cookie"] == h["Cookie"]


def test_the_captured_jar_is_cached_beside_the_token():
    """jar 与 token 是**一套**：L1 与 L2 都一起写，跨 worker/重启同样自洽。"""
    warm = FakeResp(200, "ok", headers={"Set-Cookie": "acw_tc=x1; Path=/"})
    http = FakeHttp(_signin_ok("jwt-new"),
                    routes={"chat.qwen.ai/auth": warm})
    ctx = FakeCtx(options={}, http=http, key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))

    doc = json.loads(ctx.cache.store[q._signin_cache_key("user@mail.test|passw0rd")])
    assert doc["jar"] == "acw_tc=x1; token=jwt-new"
    assert q._signin_entry("user@mail.test|passw0rd")["jar"] == doc["jar"]


def test_an_old_cache_entry_without_a_jar_falls_back_to_the_old_assembly():
    """捕获特性之前写进 L2 的条目没有 jar：退回旧拼装，行为不回归。"""
    http = FakeHttp(FakeResp(200, json.dumps(CHAT_OK)))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    _seed_l2(ctx, "user@mail.test|passw0rd", "jwt-cached", q.time.time())
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Cookie"] == "fp=1; token=jwt-cached"
    signins = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]
    assert signins == [], "L2 命中不该再登"


def test_a_refused_signin_clears_a_stale_captured_jar():
    """重登被拒 ⇒ 空 jar 写回 L2：旧 jar 随失效 token 一起作废，不许复活。"""
    refused = FakeResp(200, json.dumps({"detail": "nope"}))
    http = FakeHttp(FakeResp(200, json.dumps(CHAT_OK)),
                    routes={"auths/signin": refused})
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key="user@mail.test|wrong")
    _seed_l2(ctx, "user@mail.test|wrong", "stale-jwt",
             q.time.time() - q._SIGNIN_TTL - 1,
             jar="acw_tc=s; token=stale-jwt")
    asyncio.run(q.transform(ctx, {}, "auth"))

    doc = json.loads(ctx.cache.store[q._signin_cache_key("user@mail.test|wrong")])
    assert doc["jwt"] == "" and doc["jar"] == ""


def test_pair_form_signin_failure_falls_back_to_guest():
    """signin 被拒 → 回落 guest 门：旧 token 清掉、按 guest 凭据校验。

    guest 门的凭据是设备指纹（bx_ua/bx_umidtoken）——jar 里没有时就明确
    channel_config_error，而不是静默用一个注定被 x5sec 拦的请求。
    """
    http = FakeHttp(FakeResp(200, json.dumps({"detail": "invalid credentials"})))
    ctx = FakeCtx(options={"cookie": "fp=1; token=old",
                           "bx_ua": "ua-g", "bx_umidtoken": "um-g"},
                  http=http, key="user@mail.test|wrong")
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert "token=old" not in h["Cookie"]
    assert h["bx-ua"] == "ua-g" and h["bx-umidtoken"] == "um-g"


def test_waf_challenge_on_signin_is_not_read_as_success():
    """挑战页是 200 + text/html，body 里没有账号记录 —— 必须判成失败并写明原因。"""
    challenge = FakeResp(200, '<!doctype html><meta name="aliyun_waf_aa" content="x">',
                         headers={"Content-Type": "text/html"})
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=FakeHttp(challenge), key="user@mail.test|passw0rd")
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Referer"].endswith("/c/guest")           # 回落 guest 门
    notes = [n for n in ctx.logfire.notes if n.get("signin") == "failed"]
    assert notes and "WAF" in notes[0]["reason"]


def test_failed_signin_is_not_retried_on_every_request():
    """该端点有 IP 级频率墙（实测 ~12 次/6 分钟）：失败后按请求重试 = 自己撞墙。"""
    refused = FakeResp(200, json.dumps({"detail": "nope"}))
    http = FakeHttp(FakeResp(200, json.dumps(CHAT_OK)),
                    routes={"auths/signin": refused})
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key="user@mail.test|wrong")
    asyncio.run(q.transform(ctx, {}, "auth"))
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    signins = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]
    assert len(signins) == 1, "冷却窗内的第二次尝试必须被闸住"


def test_cooldown_expiry_allows_a_new_attempt():
    """窗口过了就再试一次：墙是 IP 级的，等它自己退是最省事的恢复手段。

    时间要**两层一起**往前走 —— 只看 L1 的 ts 不够，L2 里那份时间戳同样会被读到。
    """
    http = FakeHttp(FakeResp(200, json.dumps({"detail": "nope"})))
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key="user@mail.test|wrong")
    asyncio.run(q.transform(ctx, {}, "auth"))
    past = q.time.time() - q._SIGNIN_RETRY_COOLDOWN - 1
    q._signin_entry("user@mail.test|wrong")["ts"] = past
    _seed_l2(ctx, "user@mail.test|wrong", "", past)
    # 跨账号节流的戳也要跟着时间往前走，否则窗口内这一发会被它闸住
    _seed_pace(ctx, past)
    asyncio.run(q.transform(ctx, {}, "auth"))
    signins = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]
    assert len(signins) == 2


def test_401_invalidates_cached_jwt_and_resigns():
    """生成调用 401 → 作废缓存 → request 相位重新 signin → 新 jar 上 wire。"""
    routes = {"auths/signin": _signin_ok("jwt-first")}
    http = FakeHttp(FakeResp(200, json.dumps(CHAT_OK)), routes=routes)
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    h = asyncio.run(q.transform(ctx, {"prompt": "x"}, "auth"))
    assert "token=jwt-first" in h["Cookie"]

    ctx.upstream_error = {"message": "Unauthorized", "upstream_status": 401}
    routes["auths/signin"] = _signin_ok("jwt-refreshed")
    body = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))

    signin = [c for c in http.calls if "auths/signin" in c["url"]]
    assert len(signin) == 2   # 401 自愈**允许越过冷却窗**（凭据已知失效）
    emitted = [e for e in ctx.emitted if "Cookie" in e.get("headers", {})]
    assert emitted and "token=jwt-refreshed" in emitted[-1]["headers"]["Cookie"]
    # 脚本把 `Authorization` 发**空**：空值＝不发（`transport.build_request` 丢弃），
    # 这正是固化下来的抑制（实测 09-22：该头一票否决）。它从不把**凭证**放进
    # 那个头 —— 凭证在 Cookie 里，位置由 X-Auth-Emit 说了算。
    assert emitted[-1]["headers"]["Authorization"] == ""
    assert "jwt-refreshed" not in emitted[-1]["headers"]["Authorization"]
    # 强制路径**不读账号那把键**：那里躺着的是同一份已失效的 token，捡回来等于白跑一趟
    assert "jwt-first" not in emitted[-1]["headers"]["Cookie"]
    assert [k for k in ctx.cache.gets if k == q._signin_cache_key(PAIR_KEY)] == [
        q._signin_cache_key(PAIR_KEY)]      # 只有 auth 相位那次冷读
    assert body["chat_mode"] == "normal"


def test_the_retried_request_phase_rebuilds_a_different_body():
    """引擎只对「重建 body 与第一发不同」才真发第二发 —— 401 自愈靠这条成立。

    body 之所以不同，是因为 chat_id 会重新铸造；这一条钉住的是那个机制，
    免得日后有人把建会话改成幂等/缓存，让自愈静默失效。
    """

    # 取个本地别名：`post(json=...)` 的形参会遮蔽模块名 `json`（踩过一次）。
    _dumps = json.dumps

    class _MintingHttp(FakeHttp):
        """每次建会话都回一个新的 chat_id —— 真实上游就是这样。"""

        def __init__(self):
            super().__init__(FakeResp(200, json.dumps(CHAT_OK)))
            self._n = 0

        def post(self, url, json=None, headers=None):  # noqa: A002 - aiohttp 形状
            if "chats/new" in url:
                self._n += 1
                self.resp = FakeResp(200, _dumps(
                    {"success": True, "data": {"id": "chat-" + str(self._n)}}))
            return super().post(url, json=json, headers=headers)

    http = _MintingHttp()
    # signin 必须回一份带 Set-Cookie 的响应，否则这一段根本走不到 401 自愈
    http.routes["auths/signin"] = _signin_ok("jwt-1")
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    first = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    ctx.emitted = []
    ctx.upstream_error = {"message": "Unauthorized", "upstream_status": 401}
    second = asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert first["chat_id"] != second["chat_id"]
    assert first != second, "body 逐字节相同 ⇒ 引擎不会发第二发，自愈等于没做"


def test_a_waf_challenge_backs_off_longer_than_a_refusal():
    """实测：墙持续 >5 分钟（触发后 6 分钟复测仍是挑战页）⇒ 按 300 s 的节奏重试
    等于每次都在墙上再撞一下。挑战页与"口令不对"必须分档退避。"""
    challenge = FakeResp(200, '<!doctype html><meta name="aliyun_waf_aa" content="x">',
                         headers={"Content-Type": "text/html"})
    http = FakeHttp(challenge)
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))
    writes = {key: ex for key, _raw, ex in ctx.cache.sets}
    assert writes[q._signin_cache_key("user@mail.test|passw0rd")] == int(
        q._SIGNIN_WALL_COOLDOWN)
    assert q._SIGNIN_WALL_COOLDOWN > q._SIGNIN_RETRY_COOLDOWN
    # 窗口内再来一发：不该再登
    ctx.http = http = FakeHttp(_signin_ok("jwt-later"))
    asyncio.run(q.transform(ctx, {}, "auth"))
    assert not [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]


def test_a_waf_challenge_page_blocks_the_channel_for_longer():
    """WAF 挑战页是**出口/会话级**的门（实测：同一身份直连能过 chats/new，换 6 个池子出口 6/6 被拦），
    补头补 jar 都不解决 ⇒ 退避要比 x5sec 更长，且报错要点明"等冷却或换出口"。"""
    html = ('<!doctype html><meta name="aliyun_waf_aa" content="x">'
            "<meta name=\"aliyun_waf_bb\" content=\"y\">")
    ctx = FakeCtx(options={"cookie": "fp=1"},
                  http=FakeHttp(FakeResp(200, html,
                                         headers={"Content-Type": "text/html"})),
                  key="user@mail.test|passw0rd")
    with pytest.raises(AssertionError):
        asyncio.run(q._post_json(ctx, "https://chat.qwen.ai/api/v2/chats/new",
                                 {"title": "x"}, {}, "chats/new"))
    assert "WAF 挑战页" in ctx.failed[0]
    assert "换出口" in ctx.failed[0]
    remaining, why = q._blocked_remaining(ctx)
    assert remaining > q._X5SEC_COOLDOWN and "WAF" in why

    blocked = FakeCtx(options={"cookie": "fp=1"}, key="user@mail.test|passw0rd")
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(blocked, {}, "request"))
    assert "被上游挡着" in blocked.failed[0]
    assert blocked.http.calls == []


def test_a_non_waf_non_json_failure_does_not_block_the_channel():
    """别把一次性故障也当成"被挡"：只有挑战页才记窗口。"""
    ctx = FakeCtx(options={"cookie": "fp=1"},
                  http=FakeHttp(FakeResp(200, "totally not json")),
                  key="user@mail.test|passw0rd")
    with pytest.raises(AssertionError):
        asyncio.run(q._post_json(ctx, "https://chat.qwen.ai/api/v2/chats/new",
                                 {"title": "x"}, {}, "chats/new"))
    assert q._blocked_remaining(ctx)[0] == 0


def test_signin_cache_respects_the_measured_token_life():
    """TTL：JWT 实测 30 天有效期，缓存取 6 天（大余量），到期自动重新 signin。"""
    assert q._SIGNIN_TTL == 6 * 86400.0
    assert q._SIGNIN_TTL < 30 * 86400.0


def test_every_signin_hop_carries_the_browser_identity():
    """实战踩到的 bug：只抄参考实现的 per-call 头 ⇒ signin 带着 aiohttp 默认 UA 出去
    ⇒ WAF 挑战 ⇒ 拿不到 token ⇒ 密钥形态**静默降级**。参考实现是 Session 级头，
    所以它的每个请求都带 UA 与 sec-ch-ua。"""
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))

    warm = [c for c in http.calls if c["url"].endswith("/auth")][0]
    signin = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")][0]
    for call in (warm, signin):
        ua = call["headers"]["User-Agent"]
        assert "Chrome/" in ua, ua
        assert "aiohttp" not in ua.lower() and "python" not in ua.lower(), ua
        assert call["headers"]["sec-ch-ua"].startswith('"Chromium"')


def test_the_channel_user_agent_is_honoured_on_the_signin_hop_too():
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1", "user_agent": "UA-from-channel"},
                  http=http, key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))
    sent = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")][0]
    assert sent["headers"]["User-Agent"] == "UA-from-channel"


def test_x5sec_with_a_token_only_jar_names_the_fix():
    """真实跑出来的：只给 token 的 jar 会被生成端点用 x5sec 拒（它要的是浏览器指纹），
    报错必须把这句话说出来，否则运营方只看到一个 502。"""
    ctx = FakeCtx(options={"cookie": ""}, key=JWT)
    payload = {"ret": ["FAIL_SYS_USER_VALIDATE", "RGV587_ERROR::SM::哎哟喂"]}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert "x5sec" in ctx.failed[0]
    assert "浏览器指纹" in ctx.failed[0]


def test_a_full_jar_gets_no_thin_jar_hint():
    ctx = FakeCtx(options={"cookie": "ssxmod_itna=abc; acw_tc=1"},
                  key="user@mail.test|passw0rd")
    payload = {"ret": ["FAIL_SYS_USER_VALIDATE"]}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert "x5sec" in ctx.failed[0] and "浏览器指纹" not in ctx.failed[0]


def test_an_x5sec_hit_marks_the_account_and_the_next_request_fails_fast():
    """2026-09-18 实测：完整 jar 也会被 x5sec 拒（直连与换出口结果相同）⇒ 处置是
    **等**，不是补更多头。所以命中即记窗口，窗内直接失败、不发写请求。"""
    hit = FakeCtx(options={"cookie": "ssxmod_itna=abc"}, key="user@mail.test|passw0rd")
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(hit, {"ret": ["FAIL_SYS_USER_VALIDATE"]}, "response"))
    assert "风控突发" in hit.failed[0]

    nxt = FakeCtx(options={"cookie": "ssxmod_itna=abc"}, key="user@mail.test|passw0rd")
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(nxt, {}, "request"))
    assert "被上游挡着" in nxt.failed[0] and "x5sec" in nxt.failed[0]
    assert nxt.http.calls == [], "窗内不该再打上游（连发只会加深）"


def test_the_x5sec_window_is_per_account():
    hit = FakeCtx(options={"cookie": "ssxmod_itna=abc"}, key="a@mail.test|pw")
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(hit, {"ret": ["FAIL_SYS_USER_VALIDATE"]}, "response"))

    # 别人的账号不受连坐：用 JWT 形态（不需要 signin 就是登录态）
    other = FakeCtx(options={"cookie": "ssxmod_itna=abc"}, key=JWT)
    body = asyncio.run(q.transform(other, {"prompt": "a cat"}, "request"))
    assert body["messages"][0]["chat_type"] == "t2i"
    assert q._blocked_remaining(hit)[0] > 0 and q._blocked_remaining(other)[0] == 0


def test_signin_rate_limit_floor_is_documented_by_the_constant():
    """冷却窗必须远低于实测阈值（~12 次/6 分钟）才算"不会自己撞墙"。"""
    assert q._SIGNIN_RETRY_COOLDOWN >= 6 * 60 / 12


# ----------------------------------------- 双层缓存（L1 进程内 / L2 ctx.cache）

PAIR_KEY = "user@mail.test|passw0rd"


def _seed_l2(ctx, key, jwt, ts, jar=None):
    """按 L2 的真实形状预置一份（字节值 + JSON doc）。

    `jar=None` 时**省略该键** —— 那是捕获特性之前写进 L2 的**旧形状**，
    专门用来钉兼容路径（旧条目必须退回旧拼装，不许静默失效）。
    """
    doc = {"jwt": jwt, "ts": ts}
    if jar is not None:
        doc["jar"] = jar
    payload = json.dumps(doc).encode("utf-8")
    ctx.cache.store[q._signin_cache_key(key)] = payload


def _seed_pace(ctx, ts):
    """节流戳。⚠️ 它的键**已经是完整的键**，不能再过 `_signin_cache_key`（踩过）。"""
    ctx.cache.store[q._SIGNIN_PACE_KEY] = json.dumps({"ts": ts}).encode("utf-8")


def test_l2_adopts_a_token_another_worker_already_signed_in_for():
    """别的 worker 登过了就直接采用：省一次登录，也省一次撞频率墙的机会。"""
    http = FakeHttp(FakeResp(200, "must not be called"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http, key=PAIR_KEY)
    _seed_l2(ctx, PAIR_KEY, "jwt-from-l2", q.time.time())
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert "token=jwt-from-l2" in h["Cookie"]
    assert not [c for c in http.calls if "signin" in c["url"]]
    assert any(n.get("signin") == "from_cache" for n in ctx.logfire.notes)


def test_l1_hit_does_not_touch_l2():
    """热路径零 I/O：L1 命中时 ctx.cache 一次都不读。

    读的是两把键（本账号 + 跨账号节流戳），所以第二次调用后**数量不该再涨**。"""
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http, key=PAIR_KEY)
    asyncio.run(q.transform(ctx, {}, "auth"))
    after_cold = len(ctx.cache.gets)
    assert q._signin_cache_key(PAIR_KEY) in ctx.cache.gets
    asyncio.run(q.transform(ctx, {}, "auth"))        # 第二次走 L1
    assert len(ctx.cache.gets) == after_cold


def test_successful_signin_is_written_through_to_l2():
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http, key=PAIR_KEY)
    asyncio.run(q.transform(ctx, {}, "auth"))
    writes = {key: (raw, ex) for key, raw, ex in ctx.cache.sets}
    raw, ex = writes[q._signin_cache_key(PAIR_KEY)]
    assert json.loads(raw.decode("utf-8"))["jwt"] == "jwt-1"
    assert ex == int(q._SIGNIN_TTL)                  # 成功按 token 寿命存
    assert q._SIGNIN_PACE_KEY in writes              # 节流戳同时写下（另一把键）


def test_failed_signin_writes_the_cooldown_into_l2():
    """失败也穿透：别的 worker 读到的就是"刚有人撞过墙"，于是也一起被闸住。"""
    http = FakeHttp(FakeResp(200, json.dumps({"detail": "nope"})))
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key=PAIR_KEY)
    asyncio.run(q.transform(ctx, {}, "auth"))
    writes = {key: (raw, ex) for key, raw, ex in ctx.cache.sets}
    raw, ex = writes[q._signin_cache_key(PAIR_KEY)]
    assert json.loads(raw.decode("utf-8"))["jwt"] == ""
    assert ex == int(q._SIGNIN_RETRY_COOLDOWN)


def test_l2_cooldown_gates_a_process_with_a_cold_l1():
    """新进程（L1 空）读到"刚失败过" ⇒ 这一发不再尝试，直接走 guest 门。"""
    http = FakeHttp(_signin_ok("must-not-be-used"))
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key=PAIR_KEY)
    _seed_l2(ctx, PAIR_KEY, "", q.time.time())
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert not [c for c in http.calls if "signin" in c["url"]]
    assert h["Referer"].endswith("/c/guest")


def test_cache_outage_is_not_fatal():
    """Redis 挂了不该让请求失败 —— L2 的两端都是尽力而为。"""
    http = FakeHttp(_signin_ok("jwt-1"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http, key=PAIR_KEY,
                  cache=BrokenCache())
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert "token=jwt-1" in h["Cookie"]


def test_concurrent_cold_requests_single_flight_one_signin():
    """一次部署后的并发首包只该登一次：让其余请求先走 guest 门，别排队等。"""
    slow = _signin_ok("jwt-1")
    slow.delay = 0.02                     # 让并发用例真的能交错
    http = FakeHttp(slow)
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g",
                           "bx_umidtoken": "um-g"},
                  http=http, key=PAIR_KEY)

    async def burst():
        # 必须在**运行中的循环**里 gather：循环外调 gather 会报 no current event loop
        await asyncio.gather(*[q.transform(ctx, {}, "auth") for _ in range(4)])

    asyncio.run(burst())
    signins = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]
    assert len(signins) == 1, "冷缓存下的并发首包只允许一次 signin"


def test_two_accounts_do_not_evict_each_other():
    """多账号：一个进程里两个账号必须各有一格缓存。

    单槽时代实测的失败模式：B 的登录会挤掉 A 的 token ⇒ 之后每个 A 请求都重新登录，
    而该端点有 **IP 级**频率墙（12 次/6 分钟）⇒ 多账号反而更容易把出口打上墙。
    """
    account_a = "a@mail.test|pw-a"
    account_b = "b@mail.test|pw-b"
    http = FakeHttp(_signin_ok("jwt-for-a"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http, key=account_a)

    asyncio.run(q.transform(ctx, {}, "auth"))          # A 登一次
    ctx.http = http = FakeHttp(_signin_ok("jwt-for-b"))
    ctx.key = account_b
    # B 立刻来会被**跨账号节流**挡住（那是另一条门禁，见下一个用例）；这里先把戳做旧，
    # 好让 B 真的登一次，才测得到"两格互不挤掉"。
    _seed_pace(ctx, q.time.time() - q._SIGNIN_MIN_INTERVAL - 1)
    asyncio.run(q.transform(ctx, {}, "auth"))          # B 登一次
    ctx.key = account_a
    headers = asyncio.run(q.transform(ctx, {}, "auth"))  # 再回 A：必须命中 A 的缓存

    signins = [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]
    assert len(signins) == 1, "回到 A 时不该再登录（B 的登录不能挤掉 A 的）"
    assert "token=jwt-for-a" in headers["Cookie"]


def test_a_shared_pace_keeps_account_cold_starts_from_bursting():
    """跨账号节流：实测 8 账号冷启动一轮就把出口打到 WAF 挑战页（第 7、8 个账号拿不到
    token），所以"每账号一个冷却窗"不够 —— 还必须有**共用**的尝试间隔。"""
    cache = FakeCache()
    http = FakeHttp(_signin_ok("jwt-a"))
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g", "bx_umidtoken": "um-g"},
                  http=http, key="a@mail.test|pw", cache=cache)
    asyncio.run(q.transform(ctx, {}, "auth"))      # A 登一次，写下节流戳

    ctx.key = "b@mail.test|pw"                     # 紧接着 B：必须被节流
    ctx.http = http = FakeHttp(_signin_ok("jwt-b"))
    asyncio.run(q.transform(ctx, {}, "auth"))
    assert not [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]


def test_the_pace_stamp_expires_so_the_next_account_signs_in():
    cache = FakeCache()
    cache.store[q._SIGNIN_PACE_KEY] = json.dumps(
        {"ts": q.time.time() - q._SIGNIN_MIN_INTERVAL - 1}).encode("utf-8")
    http = FakeHttp(_signin_ok("jwt-b"))
    ctx = FakeCtx(options={"cookie": "fp=1"}, http=http,
                  key="b@mail.test|pw", cache=cache)
    asyncio.run(q.transform(ctx, {}, "auth"))
    assert [c for c in http.calls if c["url"].endswith("/v2/auths/signin")]


# ------------------------------- token 服务（B 方案：登录走工具侧 + 代理轮换出口）

SERVICE_URL = "http://token-svc.test/token"


def test_a_token_service_replaces_the_signin_call():
    """B 方案：登录交给工具侧（它走代理池、每条连接换出口），脚本不自己登
    ⇒ 适配器这一侧不会出现在 signin 的流量里，墙与我们无关。"""
    service = FakeResp(200, json.dumps({"token": "jwt-from-service"}))
    http = FakeHttp(_signin_ok("jwt-direct-should-not-be-used"),
                    routes={"token-svc.test": service})
    ctx = FakeCtx(options={"cookie": "fp=1", "token_url": SERVICE_URL},
                  http=http, key="user@mail.test|passw0rd")

    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    assert "token=jwt-from-service" in headers["Cookie"]
    assert not [c for c in http.calls if "signin" in c["url"]]
    assert not [c for c in http.calls if c["url"].endswith("/auth")]   # 也不预热
    asked = [c["url"] for c in http.calls if "token-svc.test" in c["url"]]
    assert len(asked) == 1 and "account=user%40mail.test" in asked[0]


def test_the_token_service_token_is_cached_for_later_requests():
    service = FakeResp(200, json.dumps({"token": "jwt-from-service"}))
    http = FakeHttp(_signin_ok("unused"), routes={"token-svc.test": service})
    ctx = FakeCtx(options={"cookie": "fp=1", "token_url": SERVICE_URL}, http=http,
                  key="user@mail.test|passw0rd")
    asyncio.run(q.transform(ctx, {}, "auth"))
    asyncio.run(q.transform(ctx, {}, "auth"))
    hits = [c for c in http.calls if "token-svc.test" in c["url"]]
    assert len(hits) == 1, "L1 命中后不该再问服务（每问一次就是一个网络跳）"


def test_token_service_accounts_are_not_paced_against_each_other():
    """服务路径**不适用**跨账号节流：出口在服务那边轮换，卡 45 s 只会让 8 个账号
    又排成 6 分钟的队（这正是节流存在的理由被绕开的地方）。"""
    service = FakeResp(200, json.dumps({"token": "jwt-svc"}))
    http = FakeHttp(_signin_ok("unused"), routes={"token-svc.test": service})
    ctx = FakeCtx(options={"cookie": "fp=1", "token_url": SERVICE_URL}, http=http,
                  key="a@mail.test|pw")
    asyncio.run(q.transform(ctx, {}, "auth"))
    ctx.key = "b@mail.test|pw"
    asyncio.run(q.transform(ctx, {}, "auth"))
    assert len([c for c in http.calls if "token-svc.test" in c["url"]]) == 2


def test_a_token_service_failure_falls_back_to_guest_and_says_why():
    service = FakeResp(503, json.dumps({"error": "walled egress"}))
    http = FakeHttp(_signin_ok("unused"), routes={"token-svc.test": service})
    ctx = FakeCtx(options={"cookie": "fp=1", "bx_ua": "ua-g", "bx_umidtoken": "um-g",
                           "token_url": SERVICE_URL}, http=http,
                  key="a@mail.test|pw")

    headers = asyncio.run(q.transform(ctx, {}, "auth"))
    assert headers["Referer"].endswith("/c/guest")            # 回落访客门
    assert not [c for c in http.calls if "signin" in c["url"]]  # 服务挂了也不自己登
    note = ctx.logfire.notes[-1]
    assert note.get("signin") == "failed"
    assert "token service" in (note.get("reason") or "")


# ------------------------------------------- 身份服务（identity_url，访客态轮换）

IDENTITY_URL = "http://127.0.0.1:8791/identity?token=s3cret"
FRESH = {"cookie": "aui=fresh; ssxmod_itna=abc", "bx_ua": "234!fresh",
         "bx_umidtoken": "T2gAfresh"}


def _identity_http(body, status=200):
    """`/identity` 走设定响应，其余（chats/new）照常返回建会话成功。"""
    return FakeHttp(FakeResp(200, json.dumps(CHAT_OK)),
                    routes={"/identity": FakeResp(status, json.dumps(body))})


def test_identity_url_replaces_the_configured_fingerprint():
    """轮换的语义就是「取回的那份覆盖配置的那份」—— 否则永远烧同一个身份。"""
    ctx = FakeCtx(options={**CREDS, "identity_url": IDENTITY_URL},
                  http=_identity_http(FRESH))
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Cookie"] == FRESH["cookie"]
    assert h["bx-ua"] == FRESH["bx_ua"] and h["bx-umidtoken"] == FRESH["bx_umidtoken"]
    assert [c["url"] for c in ctx.http.calls] == [IDENTITY_URL]
    # 取回后合并进 ctx.options，request 相位读到的是同一份（无跨请求状态）
    assert ctx.options["cookie"] == FRESH["cookie"]


def test_identity_is_fetched_once_per_request_not_twice():
    """一次请求取一次身份：重复取会白白多烧一份身份的额度。"""
    ctx = FakeCtx(options={**CREDS, "identity_url": IDENTITY_URL},
                  http=_identity_http(FRESH))
    asyncio.run(q.transform(ctx, {}, "auth"))
    asyncio.run(q.transform(ctx, {"prompt": "x"}, "request"))
    assert len([c for c in ctx.http.calls if "/identity" in c["url"]]) == 1


def test_no_identity_url_leaves_the_channel_alone():
    ctx = FakeCtx()
    asyncio.run(q.transform(ctx, {}, "auth"))
    assert [c["url"] for c in ctx.http.calls] == []      # 一个额外调用都没有


def test_partial_identity_is_refused_loudly():
    """残缺身份绝不能静默使用：它省掉的那几个字段会沿用渠道里那份旧的，
    于是"轮换"没轮换、还照旧烧旧身份的额度（这正是 identity_url 要消灭的失败模式）。"""
    ctx = FakeCtx(options={**CREDS, "identity_url": IDENTITY_URL},
                  http=_identity_http({"cookie": "aui=fresh"}))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {}, "auth"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "incomplete identity" in ctx.failed[0]
    assert "bx_ua" in ctx.failed[0] and "bx_umidtoken" in ctx.failed[0]


def test_identity_endpoint_failure_is_upstream_error():
    http = FakeHttp(FakeResp(500, "boom"),
                    routes={"/identity": FakeResp(500, "boom")})
    ctx = FakeCtx(options={**CREDS, "identity_url": IDENTITY_URL}, http=http)
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, {}, "auth"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "identity endpoint" in ctx.failed[0]


def test_identity_url_travels_in_options_not_in_headers():
    """identity_url 是渠道选项，不许泄漏到上游请求头里。"""
    ctx = FakeCtx(options={**CREDS, "identity_url": IDENTITY_URL},
                  http=_identity_http(FRESH))
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert not [k for k, v in h.items() if IDENTITY_URL in str(v)]


# ------------------------- 输入侧：写类请求头 + t2i 的时间戳口径（2026-09-19）
# 依据：参考仓 `qwen-chat-api.md` 的「写类请求必备头」附录与 §10.19 的根因更正 ——
# 同一账号、同一出口下，头不全的那套**必现** RGV587，补齐 `biz-api::build_headers`
# 那套头则连续 3 次成功出图。本仓没有干净出口，所以这里钉的是「与证据一致」，
# 不是「已实测出图」（见 `reports/2026-09-18_qwen-guest-blocked`）。


def test_write_path_headers_carry_the_measured_reference_set():
    ctx = FakeCtx()
    h = asyncio.run(q.transform(ctx, {}, "auth"))
    assert h["Accept"] == "application/json"
    assert h["Connection"] == "keep-alive"
    assert h["X-Accel-Buffering"] == "no"
    # 补的是差集：抓包已有的字段不能被顺手改掉（`Content-Type` 由 aiohttp 的
    # `json=` 带，不在这里 —— 2026-09-19 实测过出线头）。
    assert h["version"] == "0.2.0" and h["source"] == "web"
    assert h["Sec-Fetch-Site"] == "same-origin" and h["bx-v"] == "2.5.37"
    assert "Content-Type" not in h


def test_t2i_timestamps_are_milliseconds_in_both_places():
    """`chats/new` 一直是毫秒，而消息体曾经是秒 —— 后者与抓包和两处参考实现都不符。"""
    ctx = FakeCtx()
    body = asyncio.run(q.transform(ctx, {"prompt": "a cat"}, "request"))
    ts = body["messages"][0]["timestamp"]
    assert ts == body["timestamp"]
    assert 10 ** 12 < ts < 10 ** 14          # 毫秒量级（秒只有 10 位）


# -------------------------------------- 输出侧：形态、死链与会话兜底（2026-09-19）

URL1 = "https://cdn.qwenlm.ai/output/x/1.png"
INPUT_URL = "https://cdn.qwenlm.ai/output/x/input.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
#: 有 SSE 帧、但没有任何图片 URL —— 会走兜底取图的那条路径。
NO_PICTURE = b'data: {"choices":[{"delta":{"content":"no picture"}}]}\n'


def _drive(ctx, payload=None, response_format=None):
    """先跑请求相位（`_REQUESTS` 里的会话 id 与形态由它写入），再跑响应相位。"""
    body = {"prompt": "a cat"}
    if response_format:
        body["response_format"] = response_format
    asyncio.run(q.transform(ctx, body, "request"))
    return asyncio.run(q.transform(ctx, payload, "response"))


def _gets(ctx):
    """只取 GET：`FakeHttp.post` 记的是 url/json/headers，`get` 只记 url + kw。"""
    return [c for c in ctx.http.calls if "json" not in c]


def test_the_carrier_defaults_to_the_vendors_own_url():
    """什么都没说 ≠ 要 base64：不说就原样转出，一次下载都不发生。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), blobs={URL1: PNG})
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    assert ctx.downloads == []


def test_b64_json_downloads_each_image_once():
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), blobs={URL1: PNG})
    out = _drive(ctx, response_format="b64_json")
    assert out["data"] == [{"b64_json": ctx.encode_b64(PNG)}]
    assert "url" not in out["data"][0]
    assert ctx.downloads == [URL1]


def test_a_dead_link_is_a_loud_failure_when_base64_is_asked():
    """实测：3.0-pro 会给回取到 404 / 226 字节 HTML 的死链。要 base64 时它必须报错,
    而不是把一个取不回图的链接当成功交出去（没登记字节 ⇒ 桩按真件的拒绝形状报错）。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"))
    with pytest.raises(UpstreamError):
        _drive(ctx, response_format="b64_json")
    assert ctx.downloads == [URL1]


def test_b64_json_reaches_a_json_reply_with_items_too():
    """非 SSE 的 `data[]` 回复同样认调用方要的形态，且 extras 不被吃掉。"""
    ctx = FakeCtx(blobs={URL1: PNG})
    reply = {"created": 7, "data": [{"url": URL1, "revised_prompt": "p"}]}
    out = _drive(ctx, payload=reply, response_format="b64_json")
    assert out["created"] == 7
    assert out["data"] == [{"revised_prompt": "p",
                            "b64_json": ctx.encode_b64(PNG)}]


HISTORY = {
    "success": True,
    "data": {"chat": {"history": {"messages": {
        "u1": {"role": "user", "content": "a cat", "content_list": []},
        # `extra` 实测会被显式置为 null（参考探针为此踩过 AttributeError）——
        # 取图不许碰它，这里顺手钉住形状。
        "a1": {"role": "assistant", "content": "",
               "content_list": [{"phase": "image_gen", "status": "finished",
                                 "content": URL1, "extra": None}]},
    }}}},
}


def _recovery_http(history=None, chat_resp=None):
    return FakeHttp(
        FakeResp(200, json.dumps(CHAT_OK)),
        routes={"/v2/chats/chat-1":
                chat_resp or FakeResp(200, json.dumps(history or HISTORY))})


def test_a_url_less_stream_recovers_the_persisted_image():
    """t2i 没有异步任务接口，会话历史是唯一的事后取图窗口 —— 它只读、零额度。

    这条路径把「已经付过费的生成被报成 502」变成「把图取回来」。
    """
    ctx = FakeCtx(raw=NO_PICTURE, http=_recovery_http())
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    (get,) = _gets(ctx)
    assert get["url"].endswith("/v2/chats/chat-1")
    # 只读接口也要带指纹头：同一个上游、同一套头
    assert get["headers"]["Accept"] == "application/json"
    (note,) = [n for n in ctx.logfire.notes if n.get("stage") == "recover"]
    assert note["outcome"] == "found" and note["urls"] == 1


def test_recovery_takes_the_assistant_content_list_not_any_http_string():
    """URL 在 `content_list[].content`，**不在** `content`；user 消息的 content_list
    装的是入图 —— 取错了就是把调用方自己那张图当产物报出去。"""
    history = {"success": True, "data": {"chat": {"history": {"messages": {
        "u1": {"role": "user", "content": "edit this",
               "content_list": [{"content": INPUT_URL}]},
        # 实测里助手消息的 `content` 是空串，URL 只出现在 content_list；这里故意把
        # URL 放进去，钉住「不读 content」这件事
        "a1": {"role": "assistant", "content": INPUT_URL, "content_list": []},
        "a2": {"role": "assistant", "content": "",
               "content_list": [{"content": URL1}]},
    }}}},
    }
    ctx = FakeCtx(raw=NO_PICTURE, http=_recovery_http(history=history))
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]


def test_recovery_tolerates_a_list_of_messages():
    """上游客把 `messages` 从「以 id 为键的对象」改成数组时，最后一道兜底不该
    因为形状而失效。"""
    history = {"success": True, "data": {"chat": {"history": {"messages": [
        {"role": "assistant", "content_list": [{"content": URL1}]},
    ]}}},
    }
    ctx = FakeCtx(raw=NO_PICTURE, http=_recovery_http(history=history))
    assert _drive(ctx)["data"] == [{"url": URL1}]


def test_recovery_is_not_attempted_when_the_stream_carried_a_url():
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), http=_recovery_http())
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    assert _gets(ctx) == []


def test_no_recovery_without_a_minted_chat_id():
    """响应相位被单独调用（没有请求相位留下的会话 id）时不外呼，照旧报原错。"""
    ctx = FakeCtx(raw=NO_PICTURE, http=_recovery_http())
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert "有 SSE 但无图片 URL" in ctx.failed[0]
    assert _gets(ctx) == []


def test_recovery_failure_keeps_the_original_diagnosis():
    """兜底自己失败不能把原因改写成「兜底挂了」：图没拿到就是没拿到，且要说明
    两条路都试过了。"""
    ctx = FakeCtx(raw=NO_PICTURE,
                  http=_recovery_http(chat_resp=FakeResp(500, "boom")))
    with pytest.raises(AssertionError):
        _drive(ctx)
    assert "有 SSE 但无图片 URL" in ctx.failed[0]
    assert "会话历史里也没有" in ctx.failed[0]
    (note,) = [n for n in ctx.logfire.notes if n.get("stage") == "recover"]
    assert note["outcome"] == "http_500"


def test_x5sec_arriving_as_a_code_marks_the_same_cooldown():
    """`code` 形态（参考仓 biz-api 正是按它判的）不能只落进通用 502：不记退避窗的话,
    下一发立刻又打上游 —— 而连发正是加深惩罚的那件事。"""
    frame = ('data: {"error":{"code":"FAIL_SYS_USER_VALIDATE",'
             '"detail":"RGV587_ERROR::SM"}}\n')
    ctx = FakeCtx(raw=frame.encode("utf-8"))
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "x5sec" in ctx.failed[0]
    assert "code=FAIL_SYS_USER_VALIDATE" in ctx.failed[0]

    # 窗口真的记下了：同一个账号的下一次请求在本地就被挡住
    nxt = FakeCtx()
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(nxt, {"prompt": "x"}, "request"))
    assert "被上游挡着" in nxt.failed[0]


def test_the_request_bookkeeping_does_not_outlive_the_response():
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"))
    _drive(ctx)
    assert q._REQUESTS == {}


# ---------------------------------------------- 静默丢弃 ⇒ 500（让下游自己重试）

def _no_recovery_http():
    """兜底取图也拿不到（历史接口非 200）⇒ 逼到"上游确实什么都没给"。"""
    return _recovery_http(chat_resp=FakeResp(500, "history unavailable"))


def test_silent_drop_with_zero_data_lines_is_a_retryable_500():
    """**静默丢弃**（200 回来、0 条 data 行、会话里也没登记）⇒ **500**，不是 502/4xx。

    为什么是 500：`ctx.fail` 的约定与 `docs/06` 一致 —— **状态码就是给下游的重试信号**。
    这种形状上游什么都不说就丢了（实测还会伴随 ~0.2s 返回），最正确的处置是**让 new-api 自己重试一次**，
    而不是报成"不可重试"，也不是让调用方看到假的成功。
    """
    ctx = FakeCtx(raw=b'{"success":true}\n', http=_no_recovery_http())
    with pytest.raises(AssertionError):
        _drive(ctx)
    assert ctx.failed[1]["status"] == 500, ctx.failed
    assert ctx.failed[1]["err_type"] == "server_error"
    assert ctx.failed[1]["code"] == "upstream_error"
    assert "静默丢弃" in ctx.failed[0]


def test_silent_drop_with_a_truly_empty_body_is_also_500():
    """另一种到达形态：响应体**完全为空**（`raw_body` 为空 ⇒ 另一条分支）。同一处置。"""
    ctx = FakeCtx(raw=b"", http=_no_recovery_http())
    with pytest.raises(AssertionError):
        _drive(ctx)
    assert ctx.failed[1]["status"] == 500
    assert "静默丢弃" in ctx.failed[0]


def test_frames_but_no_url_is_still_502_not_500():
    """守卫：**有 SSE 帧但没图**（生成被中断）不是"静默丢弃" ⇒ 保持 502（不诱导下游重试）。"""
    ctx = FakeCtx(raw=NO_PICTURE, http=_no_recovery_http())
    with pytest.raises(AssertionError):
        _drive(ctx)
    assert ctx.failed[1]["status"] == 502
    assert "有 SSE 但无图片 URL" in ctx.failed[0]


def test_waf_page_is_still_502_not_500():
    """守卫：WAF 挑战页重试无用（同凭据同形态必再被拦）⇒ 保持 502。"""
    ctx = FakeCtx(raw='<!doctype html><meta name="aliyun_waf_aa">'.encode(),
                  http=_no_recovery_http())
    with pytest.raises(AssertionError):
        _drive(ctx)
    assert ctx.failed[1]["status"] == 502
    assert "WAF" in ctx.failed[0]


# ------------------------------------------- 内容审核未通过 ⇒ 400（不可重试）

def test_content_refusal_as_a_stream_error_frame_is_400_not_5xx():
    """`error.error_code == "data_inspection_failed"` ⇒ **400 `content_filter`**。

    依据与"静默丢弃 ⇒ 500"是同一条 doctrine、但方向相反：**状态码就是给下游的重试信号**。
    审核拒答**同 prompt 必再被拒** ⇒ 报 5xx 只会让 new-api 白烧一次生成，
    还让调用方以为"稍后能成"。OpenAI 里这一类就叫 `content_filter`。

    ⚠️ 该标志走的是 `error_code` 字段（**不是** `code`）—— 只读 `code` 会落进通用 502。
    """
    frame = (b'data: {"error": {"error_code": "data_inspection_failed", '
             b'"message": "\\u5185\\u5bb9\\u5ba1\\u6838\\u672a\\u901a\\u8fc7"}}\n')
    ctx = FakeCtx(raw=frame)
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["status"] == 400, ctx.failed
    assert ctx.failed[1]["code"] == "content_filter"
    assert ctx.failed[1]["param"] == "prompt"
    assert "重试必再被拒" in ctx.failed[0]


def test_content_refusal_in_the_json_envelope_is_also_400():
    """另一种到达形态：JSON 信封（`success:false` + `data.error_code`）⇒ 同样 400。"""
    payload = {"success": False,
               "data": {"error_code": "data_inspection_failed", "code": ""}}
    ctx = FakeCtx()
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["status"] == 400
    assert ctx.failed[1]["code"] == "content_filter"


def test_unknown_error_code_is_still_transient_502():
    """守卫：**认不出的** code 仍按瞬时 502（既有 doctrine：认不出的偏差要付一次快失败的代价，
    而不是把一条好身份停一天）。审核分支**只**认实测标志，不许顺手扩大。"""
    ctx = FakeCtx()
    payload = {"success": False, "data": {"code": "SomethingBrandNew", "details": "???"}}
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, payload, "response"))
    assert ctx.failed[1]["status"] == 502
    assert ctx.failed[1]["code"] == "upstream_error"


# ------------------------- 真实审核拒答帧（2026-09-19 用户提供原文）+ trace note

#: **逐字**来自上游的拒答帧（用户 2026-09-19 提供的原文），不是构造的。
REAL_CONTENT_REFUSAL = (
    'data: {"error": {"code": "data_inspection_failed", "modality": ["text"], '
    '"stage": "input", "details": "内容安全警告：输入数据可能包含不适当的内容！"}, '
    '"response_id": "a900b4c7-15e6-4812-9c82-02b2542514b9", "response_index": 0}\n'
).encode("utf-8")


def test_real_content_refusal_frame_maps_to_400_and_leaves_a_trace_note():
    """用**上游原文**钉住两件事：

    1. 标志在 `error.code`（bundle 里读到的 `error_code` 是另一拼法 ⇒ 两种都要认）；
    2. 400 `content_filter`（不可重试），且消息带上厂商原文（"内容安全警告：…"）；
    3. **trace note 自证**：`outcome=content_refused` + `modality=text` + `refused_at=input`
       ⇒ 生产里第一次真实出现时证据自动留档，不必再去构造违规请求。
    """
    ctx = FakeCtx(raw=REAL_CONTENT_REFUSAL)
    with pytest.raises(AssertionError):
        asyncio.run(q.transform(ctx, None, "response"))
    assert ctx.failed[1]["status"] == 400, ctx.failed
    assert ctx.failed[1]["code"] == "content_filter"
    assert "内容安全警告" in ctx.failed[0]           # 厂商原文透传
    assert "重试必再被拒" in ctx.failed[0]
    notes = [n for n in ctx.logfire.notes if n.get("outcome") == "content_refused"]
    assert len(notes) == 1, ctx.logfire.notes
    note = notes[0]
    assert note["error_code"] == "data_inspection_failed"
    assert note["modality"] == "text"
    assert note["refused_at"] == "input"
    assert "内容安全警告" in note["details"]


# ------------------------- rehost_url：跨渠道约定（openai/ark 同键），2026-09-22

OURS = "https://ours.example/20260922/a.png"


def test_rehost_url_swaps_the_vendors_link_for_ours():
    """开关只对 url 载体生效：上游链接经机制取回、验图、转存成我们的。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"),
                  options={**CREDS, "rehost_url": True}, stored_link=OURS)
    out = _drive(ctx)
    assert out["data"] == [{"url": OURS}]
    assert ctx.rehosts == [URL1]
    assert ctx.downloads == [], "url 载体的下载发生在机制里，脚本不再各付一次"


def test_rehost_url_defaults_off_and_the_vendors_link_rides_through():
    """默认关：原样转出、零下载 —— 这个渠道曾经的全部行为，一字不变。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), stored_link=OURS)
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    assert ctx.rehosts == []


def test_a_hand_written_rehost_value_stays_off():
    """与 openai 渠道同一纪律：只有 JSON 布尔 true 算开，字符串 "true" 不算。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), blobs={URL1: PNG},
                  options={**CREDS, "rehost_url": "true"}, stored_link=OURS)
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    assert ctx.rehosts == []


def test_rehost_without_storage_passes_the_vendors_link_through():
    """无存储（机制回 None）⇒ 上游链接原样透传。

    两件事都不做：不拿 data URI 冒充 url（那是红线），也不因为**我们的**
    存储缺失去 fail 一个本来能成的请求。
    """
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"),
                  options={**CREDS, "rehost_url": True}, stored_link=None)
    out = _drive(ctx)
    assert out["data"] == [{"url": URL1}]
    assert ctx.rehosts == [URL1]


def test_a_dead_link_fails_loudly_under_rehost():
    """死链在 rehost 下必须响亮失败（机制里验图），而不是把 404 HTML 页当成功交出去。"""
    dead = UpstreamError("Expected an image, got Content-Type 'text/html'",
                         code="image_content_type", status=400)
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"),
                  options={**CREDS, "rehost_url": True},
                  stored_link=OURS, rehost_error=dead)
    with pytest.raises(UpstreamError):
        _drive(ctx)
    assert ctx.rehosts == [URL1]


def test_rehost_keeps_extras_on_json_items():
    """非 SSE 的 data[] 回复同样换链接（`_carry_items` 出口），extras 原样保留。"""
    ctx = FakeCtx(options={**CREDS, "rehost_url": True}, stored_link=OURS)
    reply = {"created": 7, "data": [{"url": URL1, "revised_prompt": "p"}]}
    out = _drive(ctx, payload=reply)
    assert out["created"] == 7
    assert out["data"] == [{"revised_prompt": "p", "url": OURS}]


def test_b64_json_ignores_rehost():
    """rehost 是 url 载体的开关：要 base64 的路径不受它影响，也绝不双重下载。"""
    ctx = FakeCtx(raw=SSE_OK.encode("utf-8"), blobs={URL1: PNG},
                  options={**CREDS, "rehost_url": True}, stored_link=OURS)
    out = _drive(ctx, response_format="b64_json")
    assert out["data"] == [{"b64_json": ctx.encode_b64(PNG)}]
    assert ctx.rehosts == [] and ctx.downloads == [URL1]
