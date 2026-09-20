"""qwen/images@v1: chat.qwen.ai image generation (t2i) and editing (image_edit).

Channel setup (New API side):
  X-Upstream-Url: https://chat.qwen.ai/api/v2/chat/completions
  X-Script-Ref:   qwen/images@v1
  Key:            one of the account forms below -- a token JWT, `guest`, a
                  whole pasted cookie string, or `<user>|<password>`
  X-Channel-Options: {"cookie": "<full browser Cookie string>",
                      "bx_ua": "<234!... token>", "bx_umidtoken": "<T2gA...>",
                      "image_model": "qwen-image-3.0-pro",
                      "identity_url": "http://127.0.0.1:8791/identity?token=..."}
  # identity_url is guest-only and optional; it makes the channel rotate a whole
  # device identity per request (see tools/identity_service.py in this repo).

Two front doors, one script -- logged-in and guest:

  Same endpoint, same body; what differs is the identity behind the cookie and
  the three fields that follow from it. The mode is **read off the credential
  itself** (`_chat_mode`) so neither channel type has to declare it, and the
  credential requirements follow the mode instead of being one flat list:

  |                        | logged-in (`normal`)  | guest                     |
  |------------------------|-----------------------|---------------------------|
  | identity               | the `token` JWT --    | the device fingerprint:   |
  |                        | channel key or cookie | `bx-umidtoken` + `bx-ua`, |
  |                        | (§2.1/§2.2)           | no `token` (§2.6)         |
  | `chat_mode` (body)     | `"normal"`            | `"guest"`                 |
  | `Referer`              | `/c/new-chat`         | `/c/guest`                |
  | quota                  | per account           | per device identity,      |
  |                        |                       | ~4/day, IP-independent    |
  |                        |                       | (§2.7)                    |
  | `bx-ua`/`bx-umidtoken` | optional -- §2.5      | **required** -- they are  |
  |                        | variant D streamed    | the only credential there |
  |                        | with the cookie alone |                           |

  ⚠️ A logged-in channel still needs the **rest of a real browser session's jar**
  (ssxmod / acw_tc / ...): the token alone is not a device fingerprint, and the
  *generation* endpoint answers a token-only jar with x5sec (measured
  2026-09-18). §2.5's "cookie + version is enough" was measured on a t2t stream.

  **Where the account token comes from.** Two equivalent slots, and §2.1 records
  the same JWT in both: the **channel key** (a control plane sends it as
  `Authorization: Bearer <jwt>`, and `adapter.channel` strips the prefix, so the
  script sees the bare token in `ctx.key`) and the `token` entry of the cookie
  jar. The key is read first, and whichever arrives is written into the Cookie
  as `token=<...>`, **replacing** any `token=` the operator left in the jar
  string -- the vendor authenticates on the cookie, and with two tokens present
  which one wins is not something to guess at.

  **The key takes four forms**, told apart by shape alone (`_key_is_pair`,
  `_key_is_jar`) so one script serves four kinds of channel:

  | key                  | door      | what happens                                         |
  |----------------------|-----------|------------------------------------------------------|
  | `eyJ...` (a JWT)     | logged-in | used as the token; jar still comes from `cookie`      |
  | `""` (empty)         | from jar  | the jar's own `token=` decides; without one, guest    |
  | `guest`              | guest     | the jar goes out with any `token=` **removed**        |
  | a whole cookie str.  | logged-in | the string *is* the jar, verbatim; token read from it |
  | `<user>|<password>`  | logged-in | `POST /v1/auths/signin` for a JWT, cached -- below    |

  **The `<user>|<password>` form signs in.** `POST /v2/auths/signin` (v2 -- only
  the read-only account record lives on v1) takes `sha256(password)` and answers
  the JWT **in `Set-Cookie`**, never in the body, which is the account record. A
  warm-up `GET /auth` goes first so the WAF's cold-start cookies are in the
  session jar. The token is then cached module-side for `_SIGNIN_TTL` (6 days,
  against a measured 30-day life), because signing in is side-effecting and the
  endpoint is **rate-limited by IP**: roughly 12 attempts in 6 minutes trips an
  Aliyun challenge page that arrives as **200 + text/html**, lasts minutes, and
  clears only with a different egress. Hence `_SIGNIN_RETRY_COOLDOWN` -- a
  refusal is not retried per request, and the window is shared across channels
  because the wall is per IP. A refusal resolves the pair form to `guest` (the
  fallback an operator asked for, rather than a guaranteed 401), and a 401 on the
  generation call earns exactly one forced re-sign-in through the engine's single
  retry (`ctx.upstream_error`), so a rotated password heals without a redeploy.

  Two cache layers keep that cheap: the module-level dict answers the hot path
  with **no I/O at all**, and `ctx.cache` (Redis when `redis_url` is set) is read
  only when this process holds no usable token -- at most once per `_SIGNIN_TTL`
  -- so a second worker adopts the token instead of signing in again. Concurrent
  cold requests single-flight in-process, so a deploy's first burst costs one
  sign-in rather than one per worker.

  **A guest channel can rotate its identity.** Set `identity_url` and the auth
  phase fetches `{"cookie", "bx_ua", "bx_umidtoken"}` from it once per request,
  merging all three into the channel options before validation. All three or
  none: rotating only part of an identity is not a rotation, because the umid
  *is* the device and the quota is bound to it (§2.7) -- an endpoint that answers
  partially is refused loudly rather than blended with the stale fields.

  **A logged-in channel can delegate the sign-in.** Set `token_url` (e.g.
  `http://127.0.0.1:8792/token`, see `tools/token_service.py`) and the script
  asks it for `{"token": "<jwt>"}` per account (`?account=<email>`) instead of
  calling `signin` itself. Why that matters is empirical: `signin` has an
  **IP-scoped** wall, so N accounts signing in from the adapter's egress get
  challenge pages (measured 2026-09-18), while the service can sign in through a
  rotating SOCKS5 pool and hand back a stateless JWT that this egress *uses*
  without ever touching the wall. With `token_url` set, the script makes **no**
  signin and **no** warm-up call at all -- verified by counting on the wire.

  ⚠️ The engine additionally emits the channel key upstream as
  `Authorization: Bearer <key>` by default. Set **`X-Auth-Emit: none`** to
  suppress it: the web app never sends that header, so leaving it on is a
  deviation from the captured request for no benefit -- and the token already
  travels in the Cookie.

  `version: 0.2.0` goes out in both modes: §2.5's single-variable run makes it
  necessary and sufficient for a logged-in session, while the guest capture
  (§2.6) lacking it is recorded there as an **unresolved conflict** -- neither
  reading licenses dropping the header, so it is not made an option that could
  only ever be set wrong.

  The rest of the write-path header set is not decoration either, and §10.19 of
  the reference contract is the measurement that says so: with **the same
  account and the same egress**, a request missing `Sec-Fetch-*`, `Timezone`,
  `Connection` and `X-Accel-Buffering` (and spelling `Accept` as
  `text/event-stream`) was refused with x5sec *every* time, while the full
  `biz-api::build_headers` set drew three images in a row -- which retracted an
  afternoon of conclusions about rate, accounts and exit IPs, all of which had
  been drawn from an incomplete header set. Hence `Accept: application/json`,
  `Connection: keep-alive` and `X-Accel-Buffering: no` in `_headers`, next to
  the fields the captures already had. The operational form of that lesson: on
  an RGV587, compare this dict to `build_headers` field by field **first**,
  before touching credentials, egress or pacing.

Two links live behind the single canonical endpoint. Which one runs is decided
by the request itself -- an `image` in the payload means editing -- not by a
channel switch, which is the same rule the frontdoors already use:

  t2i          (no input image)  chat_type=t2i,         prompt only
  image_edit   (>=1 input image) chat_type=image_edit,  input images in files[]

Why this script needs three phases (the only one in the store that does):

  1. `auth`      -- qwen is browser-fingerprint gated: the request must carry a
     full Cookie jar plus the bx-ua / bx-umidtoken security tokens and the
     Sec-Fetch-* / sec-ch-ua client hints, or Aliyun's WAF answers with a
     challenge page (2026-09-17: completing those headers flipped the *same*
     proxy IP from blocked to drawing images). The credentials come from the
     channel options, not the client.
  2. `request`   -- both links are multi-call: `POST /v2/chats/new` (free, not
     metered, content-less) mints the chat_id the generation call needs, and
     image_edit additionally uploads each input image to the vendor's OSS
     before it can be referenced. The engine's single-upstream-call invariant is
     about the *generation* call; these auxiliary calls are the same shape as
     ctx.download_image. The generation URL itself carries `?chat_id=` which
     only exists after the mint, so the script overrides it via
     ctx.emit(url=..., query=...).
  3. `response`  -- the success path is a `text/event-stream`, which
     parse_body deliberately does not decode; the engine therefore hands the
     script `ctx.upstream_raw` (bytes) with payload=None, and this script
     parses the `data:` frames itself. A JSON body on this endpoint means an
     error frame. Two envelopes carry the same refusals, and both are read:
     `{"success": false, "data": {"code": ...}}` (the JSON refusal) and the
     bare `{"error": {"code": ...}}` frame that 3.0-pro uses for its quota
     refusal (measured 2026-09-18). The quota codes -- `RateLimited` and
     `quota_limit` -- are *overloaded*: the daily-quota wording and the
     transient "service is busy" wording share one code, so `details` decides
     between 429 (do not retry) and 502 (retry later); see `_fail_qwen_error`.
     The x5sec refusal arrives that way too, or as a `code` carrying RGV587 /
     FAIL_SYS_USER_VALIDATE, and both forms now mark the same cooldown.

     This phase also decides the reply's *shape*. A caller that asks for
     `b64_json` gets base64 -- one checked download per image, via
     `ctx.download_image` -- and a caller that asks for `url` (or says nothing,
     which is not a request for base64) gets the vendor's own link untouched.
     The download is what makes a **dead link** detectable: measured upstream
     behaviour is that `qwen-image-3.0-pro` can publish a CDN URL that fetches a
     404 / 226-byte HTML page, and a client handed that as a success has no
     picture. Judging by magic bytes rather than `Content-Type` is the same
     lesson the framework's own downloader learned (a real PNG came back as
     `application/octet-stream` in the same batch).

     And when the stream carries no URL at all, this phase reads the session
     back: `GET /v2/chats/<chat_id>`, read-only and **unmetered**, where a
     finished generation is persisted (`qwen-chat-api.md` §5.1.5 -- t2i has no
     async task endpoint, so this is the only window onto it). It runs on the
     failing path only; a request that would otherwise be reported as a failure
     with the image sitting upstream gets the image instead.

What this script owns, and what it borrows:

  Vendor *policy* -- the dialect on the wire (see `_resolve_size`), which
  parameters ride in `extra.meta`, which fields the edit link carries. The
  *mechanism* is borrowed: `ctx.parse_size` reconciles the size dialects
  (docs/07 §5.1, AC-35), `ctx.image_bytes`/`sniff_mime` decode inputs,
  `ctx.fanout` materialises several of them **at once** -- bounded
  (`fanout_concurrency`) and order-preserving, which §14.2.1 allows outright:
  it excludes concurrent *generation* calls, not concurrent materialisation --
  and where a channel credential goes on the wire is the engine's `X-Auth-Emit`.

  `size` in one breath: the wire form is the ratio enum; a tier is expressed as
  `image_model` + "auto"; a resolution the table cannot place goes out verbatim
  **unless the model is a `-pro` one**, where it goes out as "auto" instead
  (measured 2026-09-18: pro + explicit pixels hangs the full 300s and answers
  `internal_error`, so that request cannot succeed -- see `_resolve_size`);
  anything unrecognized becomes "auto" (never a 400). Upstream has **no 4K
  tier**, so a 4K request lands on pro at <=2688px -- reported as fact, not
  worked around. `auto` is not 1:1: §5.1.5 measured it producing 2528x1696.

  The vendor's *volunteered* metadata (`usage`, `extra.output_image_hw`) goes on
  the trace, never in the body: the client contract is OpenAI's images shape.
  `output_image_hw` is recorded as [height, width] in one place and read as
  [width, height] in another, so it is passed through as-is, not adjudicated.

  Guest identities are minted **out of process** by `tools/identity_service.py`
  (one Playwright context per identity: the umid lands in the page as
  `localStorage["lswusea"]`, and `bx-ua` comes from the page's own
  `AWSC.configFYEx`). Each identity serves ~4-5 drawings a day, which is the
  whole reason `identity_url` rotation exists.

  Not done here, on purpose: the reverse proxy's `generate_image_via_chat`
  fallback (it answers a blocked t2i with a second *generation* call -- the one
  thing docs/07 §14.2.1 excludes -- and cannot report the resolution it
  produced); text chat and the model list (this repo is images-only); multipart
  OSS upload (inputs are normalized downstream, 2 MiB single PUT, loud failure);
  in-script `bx-ua` minting (it needs a subprocess the sandbox does not have).
  ⚠️ The session read-back in the response phase is not that fallback: it is a
  GET, so it adds no generation call and cannot produce a picture the account
  was not already charged for.

Where the contract, its evidence and its operations live:
  - `docs/10_Qwen_Integration.md` -- the contract this script implements, with an
    evidence grade per part (t2i request/SSE is measured end to end; the
    image_edit link -- upload, `files[]`, the three `chat_type` fields -- was
    frontend-bundle static analysis until 2026-09-18, when the reference
    project ran it end to end: real upload + real generation, PNG out, and 20
    input pictures accepted; the STS->OSS upload link is the part biz-api
    plays for real), the failure taxonomy, and the runbook.
  - the raw measurement records stay in the reverse-proxy repo
    (`docs/upstream/qwen-{chat,login,image-vision,quota}-api.md`, probes under
    `qwen/probe/`). Change the contract -- new endpoint, new field -- and those
    are what to read first; this repo holds the conclusions, not the evidence.
"""
import datetime
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import uuid

# `partial` 而不是闭包：`ctx.fanout` 交来的是一元可调用对象，而 openai/images@v1
# 已经用这个写法表达同一件事（一个模块级函数 + 绑好 ctx），store 里读起来一致。
from functools import partial

PHASES = ("auth", "request", "response")

DEFAULT_BASE = "https://chat.qwen.ai/api"
CHAT_MODEL = "qwen3.7-plus"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

TIER_TO_MODEL = {
    "1K": "qwen-image-3.0",
    "2K": "qwen-image-3.0-pro",
    # Upstream has no 4K tier (bundle-verified); pro is the honest ceiling.
    "4K": "qwen-image-3.0-pro",
}
#: Ratio -> the absolute resolution the vendor's own UI pairs it with. The wire
#: form stays the ratio (the frontend bundle has no "*"-split branch, §5.1.6);
#: the pixels are the fact that lets a request written in the other dialect be
#: answered in this one (`ctx.parse_size` does the reconciliation). `auto` is
#: deliberately absent: it means "no size given", and §5.1.5 observed it
#: producing 2528x1696 -- so "auto == 1:1" is not a mapping.
RATIO_TO_HW = {
    "1:1": "2048*2048",
    "16:9": "2688*1536",
    "9:16": "1536*2688",
    "4:3": "2368*1728",
    "3:4": "1728*2368",
}

#: `Date().toString()` shape, e.g. "Fri Sep 18 2026 12:30:00 GMT+0800".
TIMEZONE_FMT = "%a %b %d %Y %H:%M:%S GMT%z"

#: The usage keys this endpoint has been seen to send. Kept under upstream's
#: own names rather than folded into a `width`/`height` guess: `output_image_hw`
#: is recorded as [height, width] in one place and read as [width, height] in
#: another, so the script publishes what it was told and does not adjudicate.
#: `output_width`/`output_height` are the self-describing pair.
USAGE_KEYS = ("image_count", "width", "height", "output_image_count",
              "output_width", "output_height", "output_image_type")

#: Values the channel key may carry to mean "no account". The key is the
#: bearer-style credential slot (see the docstring), so this is how an operator
#: says "this channel stands in for the guest door"; "" says the same thing
#: more quietly.
NO_TOKEN_VALUES = frozenset({"guest"})
#: Guest mode has no account behind it, so the device fingerprint *is* the
#: credential (§2.6): the jar plus these two tokens. A logged-in channel may
#: leave them out -- §2.5 variant D streamed normally with the cookie alone.
GUEST_REQUIRED_CREDENTIALS = ("bx_ua", "bx_umidtoken")
#: What an identity endpoint (`identity_url`) has to answer with. All of it:
#: rotating only part of an identity is not a rotation, because the umid *is*
#: the device and the quota is bound to it (§2.7).
IDENTITY_FIELDS = ("cookie", "bx_ua", "bx_umidtoken")

#: image_edit is the upstream chat_type for "one prompt + input pictures".
EDIT_TYPE = "image_edit"
GEN_TYPE = "t2i"
#: Single-PUT upload ceiling, aligned with the frontend's put/multipart split.
SIMPLE_PUT_LIMIT = 2 * 1024 * 1024


def _fail_config(ctx, message):
    """Unusable channel options are the operator's mistake, not the client's."""
    ctx.fail(message, code="channel_config_error")


def _account_id(ctx):
    """短、稳定、不含凭据的账号标识（键可能是整串 jar，不能直接当字典键用）。"""
    key = _declared_key(ctx)
    if ";" in key:                       # 整串 cookie：拿它内部的 token 当指纹
        key = _bearer_token(ctx) or key
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _mark_blocked(ctx, seconds, reason):
    """记下"上游把这个渠道挡着"，`seconds` 内不再发请求（见 `_WAF_COOLDOWN`）。"""
    _BLOCKED[_account_id(ctx)] = (time.monotonic() + seconds, reason)


def _blocked_remaining(ctx):
    """(还有多少秒, 原因)。0 秒 = 没被挡。"""
    until, reason = _BLOCKED.get(_account_id(ctx), (0.0, ""))
    return max(0.0, until - time.monotonic()), reason


def _fail_upstream(ctx, message):
    ctx.fail(message, code="upstream_error", status=502, err_type="upstream_error")


def _note(ctx, **attrs):
    """Best-effort trace annotation; never load-bearing.

    Everything recorded here is already reflected in the outcome, so losing it
    changes nothing -- hence no failure path of its own. An environment without
    logfire (unit tests) or with the attribute renamed records nothing and
    carries on. Note the probe is a `try` rather than `hasattr`: the script
    runtime's builtins are a whitelist, and `hasattr` is not on it.
    """
    try:
        ctx.logfire.info("qwen note", **attrs)
    except Exception:  # noqa: BLE001 - instrumentation must not fail a request
        return


# ------------------------------------------------------------------ the mode


def _cookie_token(cookie):
    """The `token` entry of a jar, or "" -- §2.1 records the same JWT in the
    cookie and in local storage, so an operator may supply either."""
    for part in str(cookie).split(";"):
        name, _, value = part.partition("=")
        if name.strip() == "token" and value.strip():
            return value.strip()
    return ""


def _declared_key(ctx):
    """The channel key, trimmed. "" means the operator left the slot alone."""
    return str(ctx.key or "").strip()


# 账号密码形态（第四种 Bearer 值）：`<user>|<password>` —— signin 换 JWT。
# 契约来自反向代理的 `qwen/probe/api_login.py`（2026-09-18 实测跑通），其中三处
# 极易搞错、而且本案三处都曾搞错：路径是 **v2**（只有只读的账号档案是 v1）、
# **token 只在 `Set-Cookie`**（body 是账号记录，里面没有 token）、挑战页是
# **200 + text/html**（不是 4xx，所以它有伪装成"登录成功"的余地）。
SIGNIN_PATH = "/v2/auths/signin"
#: The browser identity every request to this vendor must carry. Defined once
#: because the reference implementation sets these at the *session* level (so
#: all of its calls had them) while transcribing only its per-call dicts leaves
#: a request going out with aiohttp's default `Python/3.x` User-Agent -- which
#: the WAF answers with a challenge page (see `_signin_headers`).
BROWSER_HINTS = {
    "Accept-Language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24", '
                 '"Google Chrome";v="152"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

#: 预热只为拿 WAF 冷启动 cookie（acw_tc / x-ap）——它们落在 session 的 cookie jar
#: 里，后面那次 signin 才不像裸请求。实测直连 0.15 s、经代理 2.2 s，只在登录时付。
SIGNIN_WARM_PATH = "/auth"
SIGNIN_WARM_HEADERS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "sec-fetch-dest": "document", "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none", "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}
SIGNIN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://chat.qwen.ai",
    "Referer": "https://chat.qwen.ai/auth",
    "sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _browser_headers(opts, base):
    """`base` plus the browser identity, with the channel's UA when it has one."""
    head = dict(base)
    head.update(BROWSER_HINTS)
    head["User-Agent"] = opts.get("user_agent", UA)
    return head


def _signin_headers(opts):
    return _browser_headers(opts, SIGNIN_HEADERS)


def _warm_headers(opts):
    return _browser_headers(opts, SIGNIN_WARM_HEADERS)

#: L1 cache: **one entry per account**, keyed exactly like L2 (`sha256(pair)[:32]`).
#: A single slot was wrong for a channel set with several accounts -- two of them
#: would evict each other's token, every request would sign in again, and that is
#: precisely how the IP-level wall below gets tripped. Entry shape:
#: `{"jwt": str, "ts": float, "inflight": bool}`.
_SIGNIN_STATE: dict = {}
#: JWT 实测有效期 30 天（载荷 `exp` = 签发 + 30d）；缓存留大余量。
_SIGNIN_TTL = 6 * 86400.0
#: 🔴 被拒之后**不按请求重试**。该端点有 **IP 级**频率墙（实测约 12 次/6 分钟即触发；
#: 浏览器客户端同样被拦，冷却 300 s 后复测 4/4 仍失败，只有换出口 IP 才恢复），而
#: `_ensure_signin` 每请求会被调两次（auth + request 相位）⇒ 没有闸门的话，一个渠道
#: 就能把整个出口打上墙，同出口的其它用途一起受损。
#: ⚠️ 窗口是**按账号**的（多账号各自可以试一次，否则第二个账号会被别人的失败连坐）；
#: 于是**总量**＝账号数 × 1/窗口 ≈ N/5min。墙是 12 次/6 分钟，所以 N 应保持在个位数
#: （≤8 稳妥）；真要更多账号，就得把窗口调长或把出口分开。
_SIGNIN_RETRY_COOLDOWN = 300.0
#: 被**WAF 挑战页**拒（而不是"口令错"）时要退得更久：实测墙的持续时间 > 5 分钟
#: （2026-09-18：触发后 6 分钟复测仍是挑战页），按 300 s 的节奏重试等于每次都在墙上再撞一下。
_SIGNIN_WALL_COOLDOWN = 900.0
#: 🔴 x5sec 突发（RGV587）的退避窗。实测 2026-09-18：同一账号在**短时高频写请求**
#: （那天 30 分钟内 20+ 次）之后，连发三条都回 `punish` 页；而**直连与代理池出口结果
#: 完全相同** ⇒ 出口不是变量（参考仓 2026-09-17 也把"出口是根因"证伪过）。参考口径是
#: "命中后不要连发、做客户端限速 + 退避"，所以命中即记一个**按账号**的窗口，窗内直接
#: 失败并说明原因：既不给上游添柴，也不让调用方以为立刻重试有用。
#: ⚠️ 记在 L1（本进程）；要让多 worker 共担窗口，把它挪进 `ctx.cache` 即可。
_X5SEC_COOLDOWN = 120.0
#: 🔴 WAF 挑战页（HTML + `aliyun_waf`）的退避窗，比 x5sec 更长。订正一处旧认知：
#: 原注释写"它是请求指纹缺失/过期，**不是 IP 被封**"—— 2026-09-18 的抽样证伪了后半句：
#: **同一个身份**在直连出口能过 `POST /v2/chats/new`，而**换 6 个池子出口 6/6 都被滑块页拦**；
#: 参考仓也记着"写类端点有累计效应，命中即停手等冷却"。所以挑战页主要是**出口/会话级**的门，
#: 补头补 jar 都不解决，只能等或换出口 —— 因此退避给足。
_WAF_COOLDOWN = 600.0
#: 一个映射装两种"被上游挡着"：{account_id: (解除时刻, 原因)}。
_BLOCKED: dict = {}
#: 🔴 除"每账号一个冷却窗"外，还要一个**跨账号共用**的节流。理由来自实测：8 账号冷启动
#: 一轮（8 次 signin 挤在几秒内）就把出口打到阿里 WAF 挑战页，第 7、8 个账号拿不到 token
#: （2026-09-18）。间隔按实测阈值反推：墙约 12 次/6 分钟 ⇒ 理论 30 s，取 45 s 留余量
#: （≈8 次/6 分钟）。戳写进 L2，配了 Redis 就是全 worker 共用；被节流的请求按 guest 走，
#: 不排队也不重试 —— 等窗口过去下一个请求自然会登。
_SIGNIN_MIN_INTERVAL = 45.0
_SIGNIN_PACE_KEY = "adapter:qwen:signin-pace"
#: L2 的键。取的是**哈希**而不是 `<user>|<password>` 本身：L2 在有 Redis 时是共享存储，
#: 而那个串是口令。`sha256(pair)` 与 signin 本身要发给上游的材料同级，且不可逆读回。
_SIGNIN_CACHE_PREFIX = "adapter:qwen:signin:"


def _signin_cache_key(key):
    return _SIGNIN_CACHE_PREFIX + hashlib.sha256(str(key).encode()).hexdigest()[:32]


def _signin_entry(key):
    """L1 里这个账号的那一条（两个账号永远不共用一格）。"""
    return _SIGNIN_STATE.setdefault(
        _signin_cache_key(key), {"jwt": "", "ts": 0.0, "inflight": False})


async def _signin_cache_get(ctx, cache_key):
    """L2 读：`ctx.cache.get` 的字节面。**尽力而为** —— 缓存挂了不该让请求失败。

    L2 就是 `adapter.context.build_cache` 那一个：配了 `redis_url` 是 `redis.asyncio`
    （于是跨 worker 共享），没配则是进程内有界的 `TTLCache`（于是这一层等于再多一份
    进程内副本，也不糟）。两种情况 get/set 同形，所以脚本不必分支。
    """
    try:
        raw = await ctx.cache.get(cache_key)
    except Exception:  # noqa: BLE001 - 缓存不可用不是请求的错
        return None
    if not raw:
        return None
    try:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, (bytes, bytearray)) else str(raw))
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


async def _signin_cache_put(ctx, cache_key, doc, ttl):
    """L2 写，同样尽力而为。"""
    try:
        await ctx.cache.set(cache_key, json.dumps(doc).encode("utf-8"), ex=int(ttl))
    except Exception:  # noqa: BLE001
        return


def _effective_key(ctx):
    """The key that actually drives the doors, after the pair form resolves.

    `<user>|<password>` resolves to the cached JWT from signin (fetched once
    per TTL in the auth phase); when signin was refused, it resolves to
    "guest" so the credential layer falls back to the guest door -- the
    fallback the operator asked for instead of a dead 401.
    """
    key = _declared_key(ctx)
    if _key_is_pair(key):
        entry = _signin_entry(key)
        return entry["jwt"] if entry.get("jwt") else "guest"
    return key


def _set_cookie_token(raws):
    """`token=<jwt>` out of Set-Cookie values. The body has no token in it --
    it is the account record -- so never look for one there."""
    for raw in raws:
        m = re.search(r"(?:^|[;\s])token=([^;]+)", str(raw))
        if m:
            return m.group(1).strip()
    return ""


def _response_set_cookies(resp):
    """Every Set-Cookie value on a response.

    aiohttp keeps repeated headers visible through `getall`; a plain mapping
    (which is what the tests hand over) only has `get`. Both shapes are read so
    the script is not coupled to the client's type.
    """
    try:
        return [raw for raw in resp.headers.getall("Set-Cookie", []) if raw]
    except AttributeError:
        single = resp.headers.get("Set-Cookie")
        return [single] if single else []


async def _ensure_signin(ctx, key, force=False):
    """pair 形态确保缓存里有可用 JWT；TTL 到期或被 401 作废则重新 signin。

    两层缓存，**热路径零 I/O**：
      - L1 = 模块级 dict（本进程，纳秒级）。命中即返回，`ctx.cache` 一次都不碰 ——
        正常渠道 6 天内只会在 L1 未命中，所以共享缓存的 I/O 不在每次请求上。
      - L2 = `ctx.cache`（配了 Redis 就跨 worker/跨重启共享）。只在 L1 拿不到可用
        JWT 时读一次：别的 worker 已经登过，这份就直接采用，省掉一次 signin（也是
        省掉一次撞频率墙的机会）。写回是**穿透**的，成功与失败都写（失败写的是
        冷却时间戳，于是别的 worker 也一起被闸住）。

    被拒时缓存空 JWT —— 凭据层随后回落 guest 门，而不是把一个注定 401 的 token
    送上 wire —— 并在冷却窗内不再尝试（理由见 `_SIGNIN_RETRY_COOLDOWN`）。
    `force=True` 只归 401 那条自愈路径：那是一个**已知失效**的凭据，值得立刻重试一次；
    但重试本身也会刷新冷却窗，所以它同样有界。

    同进程内**单飞**：冷缓存时并发来的请求只让第一个去登，其余的按"暂时没有 token"
    走 guest 门。没有它，一次部署后的并发首包会变成 N 次 signin —— 又要等，又正好
    顶着频率墙。（脚本沙箱不给 `asyncio`，所以这里用"检查后立刻置位、中间没有 await"
    这个在单线程事件循环里成立的写法。）
    """
    if not _key_is_pair(key):
        return
    entry = _signin_entry(key)
    now = time.time()
    # ---- L1：命中就结束，不产生任何 I/O（**按账号**，见 _SIGNIN_STATE）----
    if entry.get("jwt") and now - entry["ts"] < _SIGNIN_TTL:
        return
    # ---- L2：只在 L1 没有可用 JWT、且不是在 401 自愈时读 -----------------
    # `force` 的语义是"手上这份凭据**已知失效**"。L2 里躺着的是同一份 token
    # （可能还是本进程刚写进去的），捡回来只会把死凭据再送一次；所以强制路径
    # 跳过 L2，直接重新登录，随后用新 token 覆盖 L2。
    cache_key = _signin_cache_key(key)
    if not force:
        doc = await _signin_cache_get(ctx, cache_key)
        if doc:
            entry.update(jwt=str(doc.get("jwt") or ""),
                         ts=float(doc.get("ts") or 0.0))
            if entry["jwt"] and time.time() - entry["ts"] < _SIGNIN_TTL:
                _note(ctx, stage="auth", signin="from_cache")
                return
    # 登录可以交给工具侧的 token 服务（它走代理池轮换出口，见 docs/10 §3.2）：
    # 那时脚本不自己登，也就不会把适配器的出口打上墙。
    service = str(ctx.options.get("token_url") or "").strip()
    now = time.time()
    if not force and now - entry.get("ts", 0.0) < _SIGNIN_RETRY_COOLDOWN:
        return
    # 跨账号节流：冷启动时 N 个账号的登录不能挤在中国同一秒里（实测会把出口打上墙）。
    # ⚠️ 只在"脚本自己登"这条路上闸：走 token 服务时出口在服务那边轮换，这里再卡 45 s
    # 只会让 8 个账号又变成排队 6 分钟。
    if not force and not service:
        pace = await _signin_cache_get(ctx, _SIGNIN_PACE_KEY)
        if pace and now - float(pace.get("ts") or 0.0) < _SIGNIN_MIN_INTERVAL:
            _note(ctx, stage="auth", signin="paced")
            return
    if entry.get("inflight"):
        # 同一进程里已经有人在登：本请求先按"没有 token"处理（回落 guest 门），
        # 等它写回 L1/L2 后，下一个请求就是登录态。等待只会把首包延迟叠起来。
        return
    entry["inflight"] = True
    user, password = key.split("|", 1)
    base = re.sub(r"/v2/chat/completions.*$",
                  "", ctx.upstream_url or DEFAULT_BASE)
    # `base` ends in the API prefix (".../api"), but the warm-up page lives on the
    # **origin**: the reference implementation warms `https://chat.qwen.ai/auth`
    # while signin sits at `.../api/v2/auths/signin`. Joining the path to `base`
    # produced `/api/auth` -- a 404 that the best-effort handler swallowed, so the
    # WAF's cold-start cookies never actually arrived.
    parts = urllib.parse.urlsplit(base)
    warm_url = parts.scheme + "://" + parts.netloc + SIGNIN_WARM_PATH
    token, reason = "", ""
    try:
        if service:
            # token 服务模式：登录发生在服务那侧（它走代理池，每条连接换出口），
            # 适配器这一侧完全不出现在 signin 的流量里 ⇒ 墙与我们无关。
            url = service + ("&" if "?" in service else "?") \
                + "account=" + urllib.parse.quote(user)
            async with ctx.http.get(
                    url, headers={"Accept": "application/json"}) as resp:
                status = resp.status
                text = await resp.text()
            if status == 200:
                doc = json.loads(text or "{}")
                token = str((doc if isinstance(doc, dict) else {}).get("token") or "")
                if not token:
                    reason = "token service returned no token"
            else:
                reason = ("token service HTTP " + str(status) + " ("
                          + _diagnose(text) + ")")
        else:
            # 预热 best-effort：它只决定 WAF 冷启动 cookie 的有无，失败不该终止登录。
            try:
                async with ctx.http.get(warm_url,
                                        headers=_warm_headers(ctx.options)):
                    pass
            except Exception:  # noqa: BLE001
                pass
            async with ctx.http.post(
                    base + SIGNIN_PATH,
                    json={"email": user,
                          "password": hashlib.sha256(password.encode()).hexdigest()},
                    headers=_signin_headers(ctx.options)) as resp:
                status = resp.status
                text = await resp.text()
                token = _set_cookie_token(_response_set_cookies(resp))
            if status != 200:
                reason = "HTTP " + str(status)
            elif not token:
                reason = "no token in Set-Cookie (" + _diagnose(text) + ")"
    except Exception as exc:  # noqa: BLE001 - ctx.fail raises its own shape
        reason = "unreachable: " + str(exc)[:140]
    finally:
        entry["inflight"] = False
    entry.update(jwt=token, ts=time.time())
    # 429/挑战页要与"口令不对"区分退避：后者 5 分钟足够，前者实测 >5 分钟仍在墙上。
    backoff = (_SIGNIN_TTL if token
               else _SIGNIN_WALL_COOLDOWN if "WAF" in reason
               else _SIGNIN_RETRY_COOLDOWN)
    # 穿透写回：成功了别人拿去直接用；失败了别人也一起被冷却窗闸住。
    await _signin_cache_put(ctx, cache_key,
                            {"jwt": token, "ts": entry["ts"]}, backoff)
    # 记下这次尝试的时刻，供跨账号节流用（写的是**尝试**时刻，不是成功时刻）。
    await _signin_cache_put(ctx, _SIGNIN_PACE_KEY, {"ts": entry["ts"]},
                            max(int(_SIGNIN_MIN_INTERVAL) * 2, int(backoff)))
    # 成功也记：运营方最需要知道的一句话就是"这次到底登进去了没有"。
    _note(ctx, stage="auth", signin="ok" if token else "failed", reason=reason[:160])


def _key_is_pair(key):
    """`<user>|<password>`: one "|", not a jar (no ";"), not a JWT."""
    key = str(key or "").strip()
    return "|" in key and ";" not in key


def _key_is_jar(key):
    """True when the key is a pasted full-cookie string (the third Bearer form).

    A JWT has no ";" and no "=", `guest` is a bare word; only a real jar
    string carries separators -- that asymmetry is the whole discriminator.
    """
    key = str(key or "").strip()
    return ";" in key or key.startswith("token=")


def _key_is_guest(ctx):
    """True when the key explicitly selects the guest door.

    `NO_TOKEN_VALUES` is the vocabulary for that; an empty key is not on it,
    because "" is the absence of a statement rather than a statement that there
    is no account.
    """
    return _effective_key(ctx).lower() in NO_TOKEN_VALUES


def _bearer_token(ctx):
    """The account token, from the channel key or the jar.

    A non-empty key is the operator *deciding*, so it is authoritative: a JWT
    means this token, `guest` means none -- and in neither case is the jar
    consulted afterwards, or a stale `token=` left in the string would silently
    turn a channel someone marked "guest" into a logged-in one. An empty key is
    the operator declining to decide, which is where the jar's own `token=`
    earns its keep (§2.1 records the same JWT in both places).
    """
    key = _effective_key(ctx)
    if _key_is_jar(key):
        # Third Bearer form: the operator pasted a full cookie string as the
        # key. Its own `token=` entry is the account token (a guest jar would
        # have been stripped of it by whoever assembled the string).
        return _cookie_token(key)
    if key:
        return "" if _key_is_guest(ctx) else key
    return _cookie_token(ctx.options.get("cookie"))


def _jar_parts(cookie, drop_token=False):
    """A jar string's entries, optionally without the `token=` one."""
    parts = [p.strip() for p in str(cookie or "").split(";") if p.strip()]
    if drop_token:
        parts = [p for p in parts if p.partition("=")[0].strip() != "token"]
    return parts


def _with_token(cookie, token):
    """The jar with `token=<token>` in it, every other entry untouched.

    Replacing rather than appending is the point: a stale `token=` sitting next
    to a fresh one leaves the vendor to decide which wins, and that is not a
    coin to flip on an auth path.
    """
    return "; ".join(_jar_parts(cookie, True) + ["token=" + token])


def _without_token(cookie):
    """The jar with any `token=` entry taken out."""
    return "; ".join(_jar_parts(cookie, True))


def _jar_for(ctx, cookie):
    """The jar as it should go on the wire, given what the key says.

    Only one of the three cases touches the string, and each exists for a
    reason the vendor can see: a key naming an account writes that token in
    (this vendor authenticates on the Cookie, §2.1); a key saying `guest` takes
    any token *out*, because the cookie is what the vendor reads -- leaving one
    there would authenticate as an account while every field we set says
    otherwise; and an empty key leaves the operator's jar verbatim, which is the
    case that must stay byte-identical for anyone comparing against a capture.
    """
    key = _effective_key(ctx)
    if _key_is_jar(key):
        # Third Bearer form: the pasted string IS the jar -- verbatim on the
        # wire; the operator is responsible for its freshness.
        return key
    if _key_is_guest(ctx):
        return _without_token(cookie)
    if key:
        return _with_token(cookie, key)
    return cookie


def _chat_mode(ctx, opts):
    """Which front door this channel stands in for.

    An explicit `chat_mode` wins. Otherwise it follows the credential: the two
    modes differ in identity, referer and quota, and all three follow from that
    one fact -- so deriving it means a channel never has to be told which it is,
    and a guest channel cannot be left pointing at a mode whose entry the
    vendor has since closed.
    """
    declared = str(opts.get("chat_mode") or "").strip().lower()
    if declared:
        return declared
    return "normal" if _bearer_token(ctx) else "guest"


def _referer(mode):
    return ("https://chat.qwen.ai/c/guest" if mode == "guest"
            else "https://chat.qwen.ai/c/new-chat")


def _timezone():
    """`Date().toString()` shape, live rather than the captured constant."""
    return datetime.datetime.now().astimezone().strftime(TIMEZONE_FMT)


async def _maybe_fresh_identity(ctx, opts):
    """One fresh device identity per request for a guest channel.

    `identity_url` points at an identity provider -- tools/identity_service.py
    in this repo wraps Playwright for exactly that -- and answers
    {"cookie": ..., "bx_ua": ..., "bx_umidtoken": ...}. The fetched fields are
    merged back into ctx.options *in place*, so the auth phase's validation and
    header assembly and the request phase all see the same identity with no
    cross-request state. A fetch failure is loud: quietly reusing a stale
    identity would silently burn whatever quota it has left.
    """
    url = str(opts.get("identity_url") or "").strip()
    if not url:
        return opts
    try:
        async with ctx.http.get(url) as resp:
            text = await resp.text()
        fresh = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - ctx.fail raises its own shape
        _fail_upstream(ctx, "identity endpoint failed: " + str(exc)[:140])
    if not isinstance(fresh, dict):
        _fail_upstream(ctx, "identity endpoint did not answer a JSON object: "
                            + json.dumps(fresh, ensure_ascii=False)[:160])
    missing = [k for k in IDENTITY_FIELDS if not fresh.get(k)]
    if missing:
        # Partial is not "degraded": the omitted fields would keep whatever the
        # channel was configured with, so the request would go out with a fresh
        # jar next to a stale device token -- a rotation that does not rotate,
        # which is the stale-identity burn this endpoint exists to prevent.
        _fail_upstream(ctx, "identity endpoint returned an incomplete identity, missing "
                            + ", ".join(missing))
    for k in IDENTITY_FIELDS:
        opts[k] = fresh[k]
    return opts


def _credentials(ctx):
    """Refuses an unusable channel before any upstream call.

    The two doors need different things, so the requirements differ too: the
    account door needs a token (channel key or jar) and nothing here demands more
    -- §2.5 variant D streamed with very little -- while the guest door has no
    account at all, so its device fingerprint *is* the credential.

    ⚠️ Not demanding more is deliberate, and it is not a promise that a thin jar
    works: §2.5 variant D was a **t2t stream**, and the *generation* endpoint
    refuses a token-only jar with x5sec (measured 2026-09-18 -- see the hint
    `_fail_qwen_error` attaches to exactly that body). The check stays lenient so
    the upstream keeps deciding, but an operator hitting it gets told what to fix
    rather than a bare 400 of our own making.
    """
    opts = ctx.options
    token = _bearer_token(ctx)
    mode = _chat_mode(ctx, opts)
    missing = []
    if not token and not opts.get("cookie"):
        missing.append("token (channel key) or cookie")
    if mode == "guest":
        missing += [k for k in GUEST_REQUIRED_CREDENTIALS if not opts.get(k)]
    if missing:
        _fail_config(ctx, "qwen channel (" + mode + " mode) is missing credential "
                          "option(s): " + ", ".join(missing))
    return opts


def _headers(ctx):
    """Browser-fingerprint headers, item for item with the captured request.

    The Referer follows the mode and is otherwise fixed: it is dynamic per chat
    upstream, but experiments showed the value is not enforced, while the
    Sec-Fetch-*/sec-ch-ua family and the Cookie/bx-* tokens very much are.

    The Cookie itself is `_jar_for`'s business; the rule worth restating is that
    only an explicit channel key edits the operator's string -- an empty key
    sends the jar out verbatim.
    """
    opts = ctx.options
    jar = _jar_for(ctx, opts.get("cookie") or "")
    h = {
        "bx-v": opts.get("bx_v", "2.5.37"),
        "User-Agent": opts.get("user_agent", UA),
        "Origin": "https://chat.qwen.ai",
        "Referer": _referer(_chat_mode(ctx, opts)),
        "source": "web",
        "version": "0.2.0",
        # Write-path headers, from the reference project's "必备请求头" appendix
        # and its §10.19 correction: with **the same credential and the same
        # egress**, a header set missing Sec-Fetch-*/Timezone/Connection/
        # X-Accel-Buffering (and spelling Accept as text/event-stream) produced
        # RGV587 every time, while the full `biz-api::build_headers` set drew
        # three images in a row. This script already carries Sec-Fetch-* and
        # Timezone, so the gap is exactly these three. Content-Type is *not* a
        # gap -- aiohttp sets it for `json=` (measured on the wire, 2026-09-19).
        "Accept": "application/json",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        # Both in the captures of either mode, both minted per call -- which is
        # what a browser does too, so the two calls of one request legitimately
        # carry different ids.
        "Timezone": _timezone(),
        "X-Request-Id": str(uuid.uuid4()),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    # 浏览器身份只有一份定义（UA + Accept-Language + sec-ch-ua*），signin 与预热同用。
    h.update(BROWSER_HINTS)
    # This vendor authenticates on the Cookie (§2.1), so that is where the
    # credential has to be; `_jar_for` has already reconciled key and jar.
    if jar:
        h["Cookie"] = jar
    # Sent when given, never demanded: guest mode needs them (and `_credentials`
    # has already refused without them there), logged-in mode does not.
    if opts.get("bx_ua"):
        h["bx-ua"] = opts["bx_ua"]
    if opts.get("bx_umidtoken"):
        h["bx-umidtoken"] = opts["bx_umidtoken"]
    return h


def _is_pro_model(image_model):
    """Whether this is one of the vendor's `-pro` image models.

    The explicit-pixel caveat below applies to the pro tier only: the non-pro
    models *silently ignore* a pixel pair (measured 2026-09-18) and answer
    2048x2048, which costs nothing.
    """
    return str(image_model or "").endswith("-pro")


def _resolve_size(ctx, payload, opts):
    """Returns (size, image_model, size_hw) -- one dialect in, one dialect out.

    The *parsing* half is the framework's (`ctx.parse_size`, adapter/ctxapi/
    mapping.py): recognizing a ratio enum, an absolute resolution and a tier,
    and knowing that ``"2688*1536"`` and ``"16:9"`` can be the same request, is
    the same problem for every image upstream and each one used to re-derive it.
    What stays here is this vendor's *policy*, which is where the two dialects
    differ:

      - the wire form is the ratio enum, because that is what the frontend
        sends (no "*"-split branch in the bundle, §5.1.6);
      - a resolution the table cannot place goes out verbatim -- the frontend
        never sends that form, so its acceptance upstream is unverified and it
        is reported rather than reshaped;
      - **except when the model is a `-pro` one**: measured 2026-09-18, pro +
        explicit pixels hangs for the full 300s and then answers
        `internal_error` (the non-pro models ignore the pixels instead), so the
        request cannot succeed and "auto" is sent in its place. Same preference
        for a working request over a 400 that the unrecognized case already
        makes; the discarded value goes on the trace, not down the drain;
      - a tier is expressed upstream as an image model, so it sets
        `image_model` and sends "auto";
      - anything unrecognized becomes "auto" -- a deliberate fallback rather
        than a 400: the model then picks the ratio from the prompt, which is a
        working request either way.
    """
    image_model = opts.get("image_model")
    raw = str(payload.get("size") or opts.get("size") or "auto").strip()
    reading = ctx.parse_size(raw, ratios=RATIO_TO_HW, tiers=tuple(TIER_TO_MODEL))
    if reading.tier:
        return "auto", (image_model or TIER_TO_MODEL[reading.tier]), None
    if reading.ratio or reading.hw:
        if reading.hw and not reading.ratio and _is_pro_model(image_model):
            # An explicit pixel pair that is none of the vendor's ratios. It
            # cannot be honoured on this model (see the docstring), and a hung
            # request is worse than a refused one: send the working request and
            # record what was dropped. `size_hw` still reports the pixels that
            # were asked for, so the trace shows both numbers.
            _note(ctx, stage="size_guard", requested=raw, sent="auto",
                  image_model=image_model, size_hw=reading.hw,
                  reason="pro 模型 + 显式像素会挂满 300s 再 internal_error（docs/10 §5.1）")
            return "auto", image_model, reading.hw
        return (reading.ratio or reading.hw), image_model, reading.hw
    return "auto", image_model, None


def _input_refs(payload, opts):
    """Input images from the canonical body, in order.

    `image` is a string or a list (both are canonical); empty strings are
    dropped by the frontdoors already, but a defensive filter keeps a stray
    "" from reaching ctx.image_bytes. `image_mode=first` is an operator escape
    hatch for the experiment where the vendor only honours one picture: it is
    opt-in, so the default never silently discards an input.
    """
    refs = payload.get("image")
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, (list, tuple)):
        refs = []
    refs = [r for r in refs if isinstance(r, str) and r.strip()]
    if str(opts.get("image_mode", "all")).lower() == "first":
        refs = refs[:1]
    return refs


# --------------------------------------------------------------- upload link


def _filetype_of(mime):
    """MIME -> one of the four classes getstsToken wants (frontend getFileType)."""
    low = (mime or "").lower()
    if low.startswith("image"):
        return "image"
    if low.startswith("video"):
        return "video"
    if low.startswith("audio"):
        return "audio"
    return "file"


def _ext_of(mime):
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
            "image/gif": "gif", "image/bmp": "bmp",
            "image/tiff": "tif"}.get((mime or "").lower(), "bin")


def _encode_path(path):
    if not path.startswith("/"):
        path = "/" + path
    return urllib.parse.quote(path, safe="/-_.~")


def _oss_target(sts):
    """(host, region, scheme) from a getstsToken payload. Endpoint may or may
    not carry a scheme, and may or may not already be bucket-qualified."""
    endpoint = str(sts.get("endpoint") or "").strip()
    scheme = "https"
    if endpoint.startswith("http://"):
        scheme = "http"
    for prefix in ("https://", "http://"):
        if endpoint.startswith(prefix):
            endpoint = endpoint[len(prefix):]
    endpoint = endpoint.rstrip("/")
    bucket = str(sts.get("bucketname") or "").strip()
    host = endpoint if endpoint.startswith(bucket + ".") else f"{bucket}.{endpoint}"
    region = str(sts.get("region") or "").strip()
    # Measured 2026-09-18: getstsToken returns "oss-ap-southeast-1" (with the
    # service prefix) against the global-accelerate endpoint; the signing
    # scope wants the bare region, and the prefixed form is exactly what OSS
    # rejects with "Invalid signing region in Authorization header".
    if region.startswith("oss-"):
        region = region[len("oss-"):]
    region = region or "cn-hangzhou"
    return host, region, scheme


def _v4_headers(sts, host, path, region, data, mime):
    """OSS V4 (OSS4-HMAC-SHA256) headers for a single PUT.

    The frontend initializes ali-oss with `authorizationV4: true`, and biz-api
    reports V4 being accepted with no V1 fallback needed -- so V4 is the only
    style attempted here.
    """
    sk = sts["access_key_secret"]
    ak = sts["access_key_id"]
    sts_token = sts.get("security_token") or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    date = now.strftime("%Y%m%d")
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    scope = date + "/" + region + "/oss/aliyun_v4_request"
    # Measured 2026-09-18: against the global-accelerate endpoint
    # (oss-accelerate.aliyuncs.com -- today's getstsToken hands it out) OSS
    # answers "The x-oss-content-sha256 only supports UNSIGNED-PAYLOAD" to a
    # real digest. ali-oss V4 sends the literal too, so the body hash is
    # dropped entirely; integrity rests on TLS, as the browser's own uploads.
    payload_hash = "UNSIGNED-PAYLOAD"

    # UNSIGNED-PAYLOAD mode, verbatim from OSS's own 403 CanonicalRequest
    # echo (2026-09-18): no host, no content-length among the canonical
    # headers, and the signed-headers line is EMPTY.
    signable = {
        "x-oss-content-sha256": payload_hash,
        "x-oss-date": ts,
        "content-type": mime,
    }
    if sts_token:
        signable["x-oss-security-token"] = sts_token
    names = sorted(signable)
    canonical_headers = "".join(
        k + ":" + str(signable[k]).strip() + "\n" for k in names)
    canonical_request = "\n".join([
        "PUT",
        _encode_path(path),
        "",
        canonical_headers,
        "",
        payload_hash,
    ])
    string_to_sign = "\n".join([
        "OSS4-HMAC-SHA256",
        ts,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = hmac.new(("aliyun_v4" + sk).encode(), date.encode(),
                   hashlib.sha256).digest()
    key = hmac.new(key, region.encode(), hashlib.sha256).digest()
    key = hmac.new(key, b"oss", hashlib.sha256).digest()
    key = hmac.new(key, b"aliyun_v4_request", hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out = {k: v for k, v in signable.items() if k != "host"}
    out["Authorization"] = ("OSS4-HMAC-SHA256 Credential=" + ak + "/" + scope
                            + ",Signature=" + signature)
    return out


def _build_file_item(sts, name, mime, size):
    """One element of message.files, matching the frontend's own file item.

    `file` is a browser File object, i.e. `{}` once stringified -- mirrored
    faithfully rather than omitted.
    """
    kind = _filetype_of(mime)
    fid = sts.get("file_id") or ""
    return {
        "type": kind,
        "file": {},
        "id": fid,
        "url": sts.get("file_url") or "",
        "name": name,
        "collection_name": "",
        "progress": 100,
        "status": "uploaded",
        "greenNet": "success",
        "size": size,
        "error": "",
        "itemId": fid or uuid.uuid4().hex,
        "file_type": mime,
        "showType": kind,
        "file_class": kind if kind != "file" else "document",
    }


async def _post_json(ctx, url, body, headers, what):
    """POST + JSON decode + WAF detection. Fails via ctx.fail, never returns
    an error tuple -- a caller must not be able to ignore the failure."""
    try:
        async with ctx.http.post(url, json=body, headers=headers) as resp:
            status = resp.status
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001 - ctx.fail raises its own shape
        _fail_upstream(ctx, "qwen " + what + " unreachable: " + str(exc)[:140])
    if status != 200:
        _fail_upstream(ctx, "qwen " + what + " HTTP " + str(status) + ": "
                            + text[:140])
    try:
        return json.loads(text)
    except ValueError:
        # 挑战页有两种，处置不同、都不该只报"非 JSON"：
        #   - HTML + aliyun_waf（滑块）⇒ **出口/会话级**：同一身份换出口 6/6 被拦（2026-09-18 抽样），
        #     补头补 jar 都不解决 ⇒ 退避给足；
        #   - 其它非 JSON ⇒ 当作一次性故障。
        what_happened = _diagnose(text)
        if "WAF" in what_happened:
            _mark_blocked(ctx, _WAF_COOLDOWN,
                          "WAF 挑战页（出口/会话被挑战，补头补 jar 都不解决）")
            _fail_upstream(
                ctx, "qwen " + what + " 被 WAF 挑战页拦下（" + what_happened + "）："
                "请等冷却或换出口（docs/10 §6.2），不要连发 —— 参考仓记录写类端点有累计效应")
        _fail_upstream(ctx, "qwen " + what + " returned non-JSON ("
                            + what_happened + "): " + text[:120])


async def _create_chat(ctx, base, headers, mode, chat_type):
    """POST /v2/chats/new -> chat_id. Free and content-less: it only mints the
    session id the generation call is addressed to, so it does not touch the
    one-upstream-generation-call invariant (same class as ctx.download_image).
    """
    body = {"title": "New Chat", "models": [CHAT_MODEL], "chat_mode": mode,
            "chat_type": chat_type, "timestamp": int(time.time() * 1000),
            "project_id": ""}
    data = await _post_json(ctx, base + "/v2/chats/new", body, headers, "chats/new")
    if not data.get("success"):
        _fail_qwen_error(ctx, data)
    return data["data"]["id"]


async def _prepare_input(ctx, ref):
    """Stage 1, concurrent: the bytes and their type, nothing else yet.

    Downloads carry no proxy view at all (`ctx.download_http`), so running
    these side by side cannot widen a request's footprint and costs the vendor
    nothing.
    """
    data = await ctx.image_bytes(ref)
    return data, ctx.sniff_mime(data) or "image/png"


async def _fetch_sts(ctx, base, headers, item):
    """Stage 2, serial: one upload credential per picture. Returns (sts, item).

    **Serial on purpose.** This is the one call in the materialisation that
    goes through the channel's proxy -- the OSS PUT below is on the bypass
    list -- and that proxy hands out an address per connection. Calling these
    concurrently would put a single client request on several exits at once,
    which is the very thing a per-request rotation exists to prevent.

    What it costs: measured at ~0.3 s per call, so a ten-image request spends a
    couple of extra seconds here, against a generation that takes tens of
    seconds and a persistence problem (a request that leaves from five
    addresses) that has no bound at all.
    """
    data, mime = item
    name = "input." + _ext_of(mime)
    if len(data) > SIMPLE_PUT_LIMIT:
        # Loud, not silent: the vendor path for larger bodies is multipart,
        # which this script deliberately does not implement.
        _fail_upstream(ctx, "input image is " + str(len(data)) + " bytes, above the "
                            "single-PUT limit " + str(SIMPLE_PUT_LIMIT)
                            + "; compress it first")
    sts_doc = await _post_json(
        ctx, base + "/v2/files/getstsToken",
        {"filename": name, "filesize": str(len(data)),
         "filetype": _filetype_of(mime)},
        headers, "getstsToken")
    if not sts_doc.get("success"):
        # `hop="upload"`：这一步不计费 ⇒ 额度措辞在此**不可能**成立（见 `_fail_qwen_error`）。
        _fail_qwen_error(ctx, sts_doc, hop="upload")
    sts = sts_doc.get("data") or {}
    missing = [k for k in ("access_key_id", "access_key_secret",
                           "security_token", "bucketname", "endpoint",
                           "file_path") if not sts.get(k)]
    if missing:
        _fail_upstream(ctx, "qwen upload credential missing field(s): "
                            + ", ".join(missing))
    return sts, item


async def _put_input(ctx, pair):
    """Stage 3, concurrent again: the PUT itself, straight to OSS.

    Safe to run in parallel because the destination is on the bypass list:
    these calls never touch the proxy, so they cannot add exits to the request.
    This is where the seconds are actually saved, so they stay a fanout.
    """
    sts, (data, mime) = pair
    name = "input." + _ext_of(mime)
    host, region, scheme = _oss_target(sts)
    bucket = str(sts.get("bucketname") or "").strip()
    path = str(sts.get("file_path") or "")
    url = scheme + "://" + host + _encode_path(path)
    # The signed canonical path carries the bucket segment even though the URL
    # is virtual-host style -- this is verbatim what OSS echoed back in the
    # <CanonicalRequest> of its own 403 (2026-09-18), which is the ground truth
    # whenever it and the local computation disagree.
    hdrs = _v4_headers(sts, host, "/" + bucket + "/" + path, region, data, mime)
    try:
        async with ctx.http.put(url, data=data, headers=hdrs) as resp:
            status = resp.status
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001
        _fail_upstream(ctx, "qwen OSS upload unreachable: " + str(exc)[:140])
    if status >= 400:
        _fail_upstream(ctx, "qwen OSS upload HTTP " + str(status) + ": "
                            + text[:160])
    return _build_file_item(sts, name, mime, len(data))


async def _materialise_files(ctx, base, headers, refs):
    """N input pictures -> N `messages[0].files[]` entries, order preserved.

    Three stages rather than one concurrent pass, because the exits decide the
    shape: downloads and uploads are direct (no proxy view, bypassed host) and
    stay a fanout, while `getstsToken` -- the one hop that goes through the
    channel's proxy -- runs serially so that one client request is one address.
    Each stage keeps `ctx.fanout`'s ordering, so `files[i]` still belongs to
    `refs[i]`.
    """
    prepared = await ctx.fanout(refs, partial(_prepare_input, ctx))
    credentials = [await _fetch_sts(ctx, base, headers, item) for item in prepared]
    return await ctx.fanout(credentials, partial(_put_input, ctx))


# -------------------------------------------------------------- output shape

#: Per-request bookkeeping the response phase needs from the request phase:
#: ``{request_id: {"chat_id": str, "response_format": str | None}}``. The
#: client's body is not in scope once the reply arrives -- `reply.payload` *is*
#: that reply, and for this vendor it is the SSE bytes -- so the two facts the
#: output side needs travel this way (the same shape `google/images@v1` uses for
#: its carrier). The response phase pops its entry, so nothing outlives its
#: request.
_REQUESTS: dict = {}
#: The two carriers OpenAI defines for an image item.
CARRIERS = ("url", "b64_json")


def _upstream_base(ctx, opts):
    """The API prefix every call of this vendor hangs off.

    Three calls now share it -- `chats/new`, the completion, and the read-back
    below -- and the channel (or a test double) decides where all three go, so
    it is derived from the channel's URL rather than from the hard-coded domain.
    `upstream_base` is the operator's override.
    """
    return (opts.get("upstream_base")
            or re.sub(r"/v2/chat/completions.*$", "",
                      ctx.upstream_url or DEFAULT_BASE))


def _requested_format(payload):
    """The carrier the caller asked for, or None when it said nothing.

    None means "the channel's own answer", which here is the URL the vendor has
    already published: no download, no re-upload, nothing added to the request's
    cost. Silence is not a request for base64.
    """
    value = payload.get("response_format")
    return value if value in CARRIERS else None


async def _b64_of(ctx, url):
    """One upstream URL -> base64, through the framework's checked download."""
    return ctx.encode_b64(await ctx.download_image(url))


async def _carry(ctx, urls, want):
    """The reply in the carrier `want` names; None = the vendor's own shape.

    `url` -- and saying nothing -- is the pass-through: the vendor published a
    link and re-hosting it would only add a copy. `b64_json` costs one download
    per image, and that download is also the only place a **dead link** can be
    caught. Measured upstream behaviour (`qwen-chat-api.md` §3.0 (7)):
    `qwen-image-3.0-pro` can hand back a CDN URL that fetches a **404 / 226-byte
    HTML page**, so a caller given that URL as a success has no picture at all.
    `ctx.download_image` refuses a body that is not an image -- by magic bytes,
    not by Content-Type, because in the same batch a genuine PNG came back as
    `application/octet-stream` -- which turns that silent failure into a loud
    one exactly when the caller is about to receive the bytes anyway. A caller
    that asked for a URL pays nothing extra: that fetch is its own to opt into,
    and not fetching is what this channel did before.
    """
    if want != "b64_json":
        return {"created": 0, "data": [{"url": u} for u in urls]}
    # Concurrently: these are waits rather than work, and `ctx.fanout` bounds
    # the degree (`fanout_concurrency`) while re-raising the earliest failing
    # item's own exception -- so a dead link keeps its own error rather than
    # being flattened into a generic one.
    blobs = await ctx.fanout(urls, partial(_b64_of, ctx))
    return {"created": 0, "data": [{"b64_json": b} for b in blobs]}


async def _item_b64_of(ctx, item):
    """One already-shaped `data[]` item -> the same item, link -> base64.

    Extras (`revised_prompt`, `width`, …) ride along: the body on the way in is
    a superset, and the way back has no reason to be narrower. An item with no
    http link is handed back untouched rather than dressed up as a picture.
    """
    url = item.get("url") if isinstance(item, dict) else None
    if not isinstance(url, str) or not url.startswith("http"):
        return item
    fresh = {k: v for k, v in item.items() if k != "url"}
    fresh["b64_json"] = await _b64_of(ctx, url)
    return fresh


async def _carry_items(ctx, items, want):
    """`_carry`'s counterpart for a reply that already carries `data[]` items.

    The SSE path composes its items here; a non-SSE reply arrives with them
    already made, and the caller's carrier still decides what the links become
    -- being asked for base64 and quietly receiving URLs is exactly the kind of
    gap this store keeps closing. Order is preserved and extras survive, both by
    `ctx.fanout`'s contract rather than by convention.
    """
    # A non-list `data` is passed through untouched: iterating it would produce
    # keys, and a reply this script does not understand is not one to reshape.
    if want != "b64_json" or not isinstance(items, list):
        return items
    return await ctx.fanout(items, partial(_item_b64_of, ctx))


# ------------------------------------------------------------------ response


def _data_lines(text):
    return sum(1 for line in text.splitlines()
               if line.strip().startswith("data:"))


def _diagnose(text):
    """Which of the four image-less shapes this reply has.

    Ported from the reverse proxy's `classify_empty`, because the four have
    different owners: a WAF page is a credential/fingerprint problem, RGV587 is
    a rate problem whose cooldown only grows if you keep banging, an empty
    stream is a vendor-side block, and a stream that carried frames but no URL
    is a generation that got cut. "no image URL" alone names none of them.
    """
    low = text.lower()
    if ("aliyun_waf" in low or "<!doctype" in low
            or "滑动验证" in text or "captcha" in low):
        return "WAF 拦截（HTML 验证页）—— 反爬头/凭据需重抓"
    if "rgv587" in low or "fail_sys_user_validate" in low:
        return "x5sec 风控/限流 —— 命中后不要连发，只会延长封锁"
    if not _data_lines(text):
        return "空 SSE（0 条 data 行）—— 疑似网关/风控拦截"
    return "有 SSE 但无图片 URL —— 疑似生成被中断"


#: Refusal codes that mean "quota-ish" in *either* dialect (§3.0 ④/⑤): the
#: JSON envelope uses `RateLimited`, the stream-internal one uses `quota_limit`.
QUOTA_CODES = ("RateLimited", "quota_limit")
#: The measured **daily-quota** wording. Both quota codes are overloaded -- the
#: transient "目前服务访问量较大" refusal arrives under the *same* code -- so the
#: wording, not the code, is what says "this identity is spent for the day".
#: Anything unrecognized is treated as transient on purpose: reading a spent
#: identity as transient costs one fast-failing retry, while the reverse costs
#: that identity its whole day (the reference project mis-classified by code
#: and paid exactly that).
QUOTA_WORDINGS = ("额度已用完", "额度用完", "额度已耗尽")



def _fail_qwen_error(ctx, doc, *, hop="generate"):
    """One place for every error shape the vendor uses, in either dialect.

    `hop` says **where** the refusal arrived, and it exists for exactly one
    branch: the quota one. Uploads (`getstsToken`) are **not metered** -- the
    vendor charges for the generation call, not for putting a file in the
    bucket -- so a quota-ish wording on that hop cannot mean "this identity is
    spent for the day". Measured 2026-09-19: that refusal is the overloaded
    `RateLimited` code telling us to slow down, with a budget **per identity per
    window** (20 calls for an account, 5 for a guest identity); a 20-image
    request spends the entire account window in one go. Reading it as 429 would
    bench a working identity for a day, which is the same asymmetry this module
    already refuses to accept on the generation hop (see QUOTA_WORDINGS).

    Two axes are handled here, both measured (2026-09-18):

      - **which envelope** carries the error. The JSON refusal puts it under
        `data` with `success: false`; the stream-internal one is a bare
        `{"error": {...}}` frame (`_read_stream` matches both shapes). Reading
        only the first turns a spent identity into "carried no image URL" --
        no 429, and the dead identity gets reused.
      - **what the code means**. `RateLimited` / `quota_limit` are overloaded:
        the daily-quota refusal and the transient over-load refusal share one
        code and differ only in `details`, so the code alone cannot separate
        "do not retry, this identity is done for the day" (429) from "retry
        later" (502). `details` decides; only the measured quota wording maps
        to 429, and the transient arm says so in as many words so the next
        reader does not "fix" it back.

    The x5sec shape is a bare `{"ret": [...]}` with no `data` at all, which is
    why it needs its own branch instead of falling through to "carried no image
    data" -- and it is *not* a rate-limit body, so it must not reach the quota
    arm either. That refusal has a **second delivery form**: the same
    `FAIL_SYS_USER_VALIDATE` / `RGV587` naming arriving as a `code`, which is
    what `biz-api::_stream_images` tests for (`qwen-chat-api.md` §6 lists both
    shapes side by side). Without that arm the code form lands in the generic
    502 below and, worse, records **no** cooldown -- so the very next request
    walks straight back into the burst that produced it, which is the one thing
    the reference project says not to do.
    """
    # `data` for the JSON dialect, `error` for the stream-internal one.
    data = doc.get("data") or doc.get("error") or {}
    code = str(data.get("code") or "")
    # 审核/风控类走的是 `error_code`（实测：`error.error_code == "data_inspection_failed"`），
    # 与 `code` 是两个字段；只读 `code` 会让它落进下面的通用 502。
    err_code = str(data.get("error_code") or "")
    # `details` is the JSON spelling; the measured stream frame carried `detail`.
    details = str(data.get("details") or data.get("detail") or "")
    ret = doc.get("ret")
    # 两种到达形态，处置相同（参考仓按 `code` 判，本脚本原本只按 `ret` 判）：
    # `{"ret":[...]}` 与 `code` 里带 RGV587 / USER_VALIDATE。
    code_up = str(code).upper()
    x5sec = bool(ret) or "RGV587" in code_up or "USER_VALIDATE" in code_up
    if x5sec:
        # Two different situations share this body, and telling them apart is the
        # difference between "fix your credentials" and "stop retrying":
        #   - a **token-only** jar is refused here (measured 2026-09-18: a JWT-only
        #     jar gets exactly this, and the same account works with a full jar);
        #   - a **complete browser jar** is refused too, once risk control has
        #     marked the account -- measured the same day: 3/3 refusals carrying a
        #     full 2419-char jar, **direct and through a rotating pool egress
        #     alike**, so the egress is not the variable (the reference project
        #     reached the same conclusion after first blaming the exit IP).
        # The remedy for the second case is time, not more headers: mark the
        # account so the next `_X5SEC_COOLDOWN` seconds fail fast instead of
        # feeding the burst that produced this body.
        _mark_blocked(ctx, _X5SEC_COOLDOWN, "x5sec 突发（写请求命中风控）")
        sent = _jar_for(ctx, ctx.options.get("cookie") or "")
        if sent and not _jar_parts(sent, drop_token=True):
            hint = ("（发出的 jar 里除了 token 没有别的项：本端点要的是浏览器指纹 jar，"
                    "只有 token 会被 x5sec 拒 —— 把真实浏览器会话的 cookie 一并配上）")
        else:
            hint = ("（jar 是完整的 ⇒ 更像风控突发窗口：该账号 %.0f 秒内被标记，"
                    "窗内不再发写请求 —— 参考记录：命中后连发只会加深）"
                    % _X5SEC_COOLDOWN)
        # `ret` 形态把原样数组贴出来；`code` 形态没有那个数组，就贴 code。
        shown = (json.dumps(ret, ensure_ascii=False)[:160] if ret
                 else "code=" + code)
        _fail_upstream(ctx, "qwen x5sec 风控/限流: " + shown + hint)
    if code in QUOTA_CODES or "额度" in details:
        if hop == "generate" and any(w in details for w in QUOTA_WORDINGS):
            # The daily quota, bound to the device identity rather than the IP
            # (§2.7): 429, and the caller must not retry this identity today.
            ctx.fail("qwen 生图额度已耗尽（" + (details or code) + "）",
                     code="upstream_quota_exhausted",
                     err_type="rate_limit_error", status=429)
        if hop == "upload":
            # 上传那一步**不计费**：这里出现的任何"额度"措辞都不可能是「生图额度」。
            # 它就是这个过载 code 的另一副面孔（按身份计窗、分钟级恢复，换出口无效），
            # 所以一律按瞬态处理，并把"这是什么"直接写进消息里。
            _fail_upstream(ctx, ("qwen 上传凭证被限流（%s）：上传不计费 ⇒ 与生图额度无关，"
                                 "按瞬态处理、稍后重试即可（实测按身份计窗：账号 20 次/窗口、"
                                 "访客 5 次/窗口，换出口无效）" % (details or code))[:220])
        # Same code, different meaning: the upstream is busy, not out of
        # quota. Retry-later (502) rather than 429-with-a-spent-identity,
        # because a wrong "spent" verdict benches a working identity for a day.
        _fail_upstream(ctx, ("qwen 上游暂时拒绝生成（%s）：按瞬时过载/限流处理，"
                             "稍后重试即可 —— 不是该身份当日额度耗尽，"
                             "不要换身份、不要标记周期耗尽" % (details or code))[:220])
    _fail_upstream(ctx, f"qwen error code={code}: {details}"[:220])


def _maybe_json(text):
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _read_stream(text):
    """(image URLs, vendor metadata, error frame) from an SSE body.

    Both links stream pictures the same way -- the frame's `phase` differs
    (image_gen vs image_edit) but the carrier does not, so no phase check is
    needed to find them. Three things come out of one pass:

      - URLs, taken from the frame *text*, so a frame that fails to parse is
        still read rather than dropped;
      - what the vendor volunteered about the picture (`usage`,
        `extra.output_image_hw`), under upstream's own names -- see USAGE_KEYS;
      - an error frame. A 200 can carry the same error as JSON or as a frame,
        so both dialects reach `_fail_qwen_error` instead of one of them being
        read as "no image URL".
    """
    urls, meta, error = [], {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        for m in re.finditer(r"https?://[^\s\"'\\]+", body):
            u = m.group(0)
            if "cdn.qwenlm.ai" in u and u not in urls:
                urls.append(u)
        obj = _maybe_json(body)
        if obj is None:
            continue
        # Two envelopes carry the same failures. The JSON refusal puts it in
        # `data` with `success: false`; the **stream-internal** dialect is a
        # bare `{"error": {...}}` frame -- measured 2026-09-18 on 3.0-pro,
        # whose quota refusal arrives that way (the other models use the JSON
        # one). Reading only the first envelope turns "this identity is spent"
        # into "carried no image URL": no 429, no quota code, and whatever sits
        # upstream of this script happily reuses the dead identity.
        if obj.get("success") is False or obj.get("error"):
            error = obj
        usage = obj.get("usage") or {}
        for key in USAGE_KEYS:
            if usage.get(key) is not None:
                meta[key] = usage[key]
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            hw = (delta.get("extra") or {}).get("output_image_hw")
            if hw and hw[0]:
                meta["output_image_hw"] = hw[0]
    return urls, meta, error


async def _recover_images(ctx, chat_id, why):
    """The picture the session already holds, when the stream did not carry it.

    `t2i` has no async task endpoint -- `/v2/task/status` whitelists only
    t2v/i2v/aipodcast -- but a finished generation is **persisted in the chat**,
    and `GET /v2/chats/<chat_id>` reads it back: read-only, zero quota. The
    frontend renders history through that same endpoint, so this is a contract
    rather than a coincidence (`qwen-chat-api.md` §5.1.5).

    Which makes it the honest answer to "the stream ended without a URL": the
    generation has been paid for -- this vendor meters the generation call, not
    the reads -- and this is the one way left to collect it. It runs only on the
    path that would otherwise fail, so a working request pays nothing; a request
    that genuinely produced nothing pays one GET and is then reported as the
    failure it always was. Either way the outcome goes on the trace.

    Three shape facts, all measured: the URL lives in the assistant message's
    ``content_list[].content`` and **not** in ``content`` (which is an empty
    string); the messages come back as an **object keyed by message id**; and
    `role == "assistant"` is the filter that matters, since a user message's
    content_list holds the input pictures. A plain list is accepted as well --
    a vendor may change that shape, and the last resort is the worst place to
    turn a shape change into a failure.
    """
    base = _upstream_base(ctx, ctx.options)
    url = base + "/v2/chats/" + urllib.parse.quote(str(chat_id), safe="")
    try:
        async with ctx.http.get(url, headers=_headers(ctx)) as resp:
            status = resp.status
            text = await resp.text()
    except Exception as exc:  # noqa: BLE001 - the backup path must not raise
        _note(ctx, stage="recover", outcome="unreachable", why=why[:60],
              error=str(exc)[:120])
        return []
    if status != 200:
        _note(ctx, stage="recover", outcome="http_" + str(status), why=why[:60])
        return []

    doc = _maybe_json(text) or {}
    data = doc.get("data")
    chat = (data.get("chat") if isinstance(data, dict) else None) or {}
    messages = (chat.get("history") or {}).get("messages") or {}
    if isinstance(messages, dict):
        messages = list(messages.values())
    urls = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for item in msg.get("content_list") or []:
            found = (item or {}).get("content")
            if isinstance(found, str) and found.startswith("http") and found not in urls:
                urls.append(found)
    _note(ctx, stage="recover", outcome="found" if urls else "empty",
          urls=len(urls), why=why[:60])
    return urls


def _message(payload, chat_type, size, image_model, files):
    # Milliseconds, and reused verbatim by the completion body below. The
    # captured t2i request carries `1789021247000` for both the message and the
    # body (`qwen-chat-api.md` §3.1), the two reference implementations agree
    # (`qwen/proxy/client.py::generate_image`, `biz-api::_generate_sync`), and
    # this script's own `chats/new` already sends ms -- seconds here was the one
    # place that disagreed with all three. (The t2t example in §3.2 is in
    # seconds; that is the other link's capture, so only this one moves.)
    ts = int(time.time() * 1000)
    meta = {"subChatType": chat_type}
    # image_edit has no measured size semantics: a size there is ignored, not
    # forwarded on a hunch.
    if chat_type == GEN_TYPE:
        meta["size"] = size
    if image_model:
        meta["model"] = image_model
    return {
        "id": None, "fid": str(uuid.uuid4()), "parentId": None,
        "childrenIds": [], "role": "user",
        "content": payload.get("prompt", ""), "user_action": "chat",
        "files": files, "timestamp": ts, "models": [CHAT_MODEL], "model": "",
        "chat_type": chat_type,
        "feature_config": {"thinking_enabled": False,
                           "output_schema": "phase",
                           "research_mode": "normal",
                           "auto_thinking": False,
                           "thinking_mode": "Fast", "auto_search": True},
        "extra": {"meta": meta},
        "sub_chat_type": chat_type, "parent_id": None,
    }


async def transform(ctx, payload, phase):
    if phase == "auth":
        # Returning a dict here makes the engine attach these headers to the
        # upstream call (executor: "headers emitted by the auth phase carry
        # into the request phase"). The identity endpoint (guest channels with
        # identity_url) is consulted here, once per request, and merged into
        # ctx.options in place -- the request phase reads the same dict.
        opts = await _maybe_fresh_identity(ctx, ctx.options)
        await _ensure_signin(ctx, _declared_key(ctx))
        _credentials(ctx)
        return _headers(ctx)

    if phase == "request":
        # 上游正挡着这个渠道（x5sec 突发 / WAF 挑战页）：窗内别再发写请求 —— 两者都会被连发加深。
        # 文案要说清"重试无用、还有多久、该找谁修"，否则调用方只会继续重试。
        remaining, why = _blocked_remaining(ctx)
        if remaining > 0:
            ctx.fail("qwen 渠道被上游挡着：%.0f 秒内不再发请求（原因：%s）"
                     % (remaining, why), code="upstream_error")
        # An auth failure on the previous attempt lands here: drop the cached
        # JWT, sign in again, and put the fresh jar on the wire. The engine
        # re-sends once because this phase's body changes as a result -- the
        # chat id is re-minted below, so the body is never byte-identical, which
        # is what the retry guard actually tests.
        # `ts` is deliberately *kept* when clearing: the refresh may bypass the
        # cooldown (the credential is known dead, that is worth one attempt),
        # but the attempt still refreshes the window, so a walled signin cannot
        # be hammered by a stream of 401s either.
        forced = bool(ctx.upstream_error
                      and str(ctx.upstream_error.get("upstream_status")) == "401"
                      and _key_is_pair(_declared_key(ctx)))
        if forced:
            # Clear *this account's* token and keep its timestamp: the cooldown
            # must survive the invalidation, and other accounts' tokens are none
            # of this request's business (that is the whole point of the
            # per-account L1).
            _signin_entry(_declared_key(ctx)).update(jwt="")
        await _ensure_signin(ctx, _declared_key(ctx), force=forced)
        opts = _credentials(ctx)
        mode = _chat_mode(ctx, opts)
        if _key_is_pair(_declared_key(ctx)) and ctx.upstream_error:
            # Only the Cookie. The credential belongs in the jar (that is what
            # this vendor authenticates on, §2.1), and *where* a channel key goes
            # on the wire is the engine's business (`X-Auth-Emit`) -- this
            # docstring even recommends `none` here, so re-introducing an
            # `Authorization: Bearer` header from inside the script would
            # contradict the advice it gives.
            fresh_jar = _jar_for(ctx, opts.get("cookie") or "")
            ctx.emit(headers={"Cookie": fresh_jar})
        # The session mint must follow the channel's upstream, not the real
        # domain: the channel (or a test double) decides where both calls go.
        base = _upstream_base(ctx, opts)
        headers = _headers(ctx)

        refs = _input_refs(payload, opts)
        # Materializing input pictures is bounded work, not a second generation
        # call -- `docs/07` §14.2.1 allows exactly this, and the other three
        # scripts have done it from the start. Each picture is still
        # fetched-and-uploaded *exactly once*: no retry, no extra generation.
        #
        # Since 2026-09-19 the fanout covers the ends only. The middle hop
        # (`getstsToken`) is serialised inside `_materialise_files`, because it
        # is the one call of the three that goes through a rotating exit: run it
        # concurrently and a single client request leaves from as many addresses
        # as it has pictures. The downloads and the PUTs stay concurrent -- they
        # never touch the proxy (see `_fetch_sts`).
        #
        # `ctx.fanout` preserves input order (`files[i]` belongs to `refs[i]`,
        # which the `files[]` order test pins), bounds the degree
        # (`fanout_concurrency`, 5 by default), and re-raises the earliest
        # failing item's own exception -- so the loud "above the single-PUT
        # limit" refusal still reaches the client as itself rather than being
        # flattened into a generic 502.
        files = await _materialise_files(ctx, base, headers, refs)
        chat_type = EDIT_TYPE if files else GEN_TYPE

        chat_id = await _create_chat(ctx, base, headers, mode, chat_type)
        # 会话 id 与调用方要的输出形态都得活到响应相位：那时的 payload 是**上游的
        # 响应**（本厂商是 SSE 字节），客户端请求体不在作用域里 —— 见 `_REQUESTS`。
        # 4xx 重试会重跑这一相位并覆盖同一条记录，这是对的：响应属于第二发。
        _REQUESTS[ctx.request_id] = {
            "chat_id": chat_id,
            "response_format": _requested_format(payload),
        }

        size, image_model, size_hw = _resolve_size(ctx, payload, opts)
        _note(ctx, stage="request", chat_type=chat_type, chat_mode=mode,
              size=size, size_hw=size_hw, image_model=image_model,
              input_images=len(files))
        msg = _message(payload, chat_type, size, image_model, files)
        # The generation URL only exists now that chat_id does; the query form
        # keeps the channel's X-Upstream-Url pointing at the bare endpoint.
        ctx.emit(url=base + "/v2/chat/completions",
                 query={"chat_id": chat_id})
        body = {"stream": True, "version": "2.1", "incremental_output": True,
                "chatId": chat_id, "parentId": "", "chat_id": chat_id,
                "chat_mode": mode, "model": CHAT_MODEL, "parent_id": None,
                "timestamp": msg["timestamp"], "messages": [msg]}
        if chat_type == GEN_TYPE:
            # Same rule as `_message`'s `extra.meta`: the edit link has no
            # measured size semantics, so it carries none at either level --
            # one rule in two places beats a contradiction between them.
            body["size"] = size
        return body

    # ---- response phase ----
    # reply.payload is the decoded JSON, or -- for anything parse_body skips
    # (text/event-stream included) -- the raw bytes themselves. The SSE success
    # path therefore arrives HERE as bytes and is parsed in this script.
    # ctx.upstream_raw is the same bytes (engine-side park) and covers the
    # payload-None corner; both names are checked before failing.
    #
    # 请求相位留下的两件事在这里取走（`_REQUESTS`）：会话 id（无图时兜底取图要用）
    # 与调用方要的输出形态。pop 而不是 get —— 它不该比这次请求活得更久。
    book = _REQUESTS.pop(ctx.request_id, None) or {}
    raw_body = (payload if isinstance(payload, (bytes, bytearray))
                else (ctx.upstream_raw if payload is None else None))
    if raw_body:
        text = (raw_body.decode("utf-8", "replace")
                if isinstance(raw_body, (bytes, bytearray)) else str(raw_body))
        urls, meta, error = _read_stream(text)
        _note(ctx, stage="response", urls=len(urls), **meta)
        if error is not None:
            _fail_qwen_error(ctx, error)
        if not urls and book.get("chat_id"):
            # 兜底：这次生成可能已经落进上游会话，而取回它是**只读、零额度**的。
            # 只在本来就要失败的路径上跑（理由与形状见 `_recover_images`）。
            urls = await _recover_images(ctx, book["chat_id"], _diagnose(text))
        if not urls:
            shape = _diagnose(text)
            if "空 SSE" in shape:
                # 🔴 **静默丢弃**（200 + 0 条 data 行）：上游"什么都不说就丢了" ——
                # 实测还会伴随 ~0.2s 就返回、且会话里连消息都没登记（2026-09-19，
                # 见 `reports/2026-09-19_qwen-stale-fileid/` 的第一发）。
                # `ctx.fail` 的约定与 `docs/06` 的用法一致：**状态码就是给下游的重试信号**
                # （4xx＝别重试，5xx＝可重试）⇒ 这里给 **500**，让 new-api 自己重试一次，
                # 而不是把"上游抽风"报成一个不可重试的 4xx，也不是让调用方看到假的成功。
                # 兜底取回（上面那步）已经先试过了：这条只在上游会话里也取不到时才走到。
                ctx.fail("qwen 静默丢弃（" + shape + "，会话历史里也没有）: " + text[:120],
                         code="upstream_error", err_type="server_error", status=500)
            _fail_upstream(ctx, "qwen 未给出图片 URL（" + shape
                                + "，会话历史里也没有）: " + text[:180])
        return await _carry(ctx, urls, book.get("response_format"))
    if payload is None:
        # 同一族"静默丢弃"的另一半：**响应体是空的**（连一行 data 都没有）。
        # 与上面那条"0 条 data 行"合起来才是完整的形状：上游 200 回来却什么都没说、
        # 会话里也没登记（实测 ~0.2s）⇒ 按可重试处理，让 new-api 自己重试。
        ctx.fail("qwen 静默丢弃（响应体为空，非 JSON）: 上游 200 但没有可用内容",
                 code="upstream_error", err_type="server_error", status=500)

    if isinstance(payload, dict) and payload.get("success") is False:
        _fail_qwen_error(ctx, payload)
    if isinstance(payload, dict) and payload.get("ret"):
        _fail_qwen_error(ctx, payload)

    if isinstance(payload, dict) and payload.get("data"):
        items = await _carry_items(ctx, payload["data"],
                                   book.get("response_format"))
        _note(ctx, stage="response", urls=len(items))
        return {"created": payload.get("created", 0), "data": items}
    _fail_upstream(ctx, "qwen reply carried no image data: "
                        + json.dumps(payload, ensure_ascii=False)[:180])
