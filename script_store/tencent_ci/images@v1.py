"""tencent_ci/images@v1: 数据万象图片处理（超分 + 三种抠图），detect-url wire form.

Channel setup (New API side):
  X-Upstream-Url:    https://<BucketName-APPID>.cos.<Region>.myqcloud.com/
                     (any bucket bound to 数据万象; it is the API entry only --
                     neither the source image nor the result ever lands in it)
  X-Script-Ref:      tencent_ci/images@v1
  X-Auth-Emit:       none
  Authorization:     <SecretId>|<SecretKey>   (TencentCloud key, pipe-joined;
                     a `Bearer ` prefix is fine -- the engine strips it)
  X-Model-Map:       aisuperresolution=AISuperResolution,
                     aipicmatting=AIPicMatting,
                     goodsmatting=GoodsMatting,
                     aiportraitmatting=AIPortraitMatting   (one channel, four ops)
  X-Channel-Options: {"op": "GoodsMatting"}    (optional op pin, for a channel
                     meant to serve exactly one capability)

The operation
-------------
doc 460/83793 (超分 AISuperResolution), 460/106750 (通用抠图 AIPicMatting),
460/79735 (商品抠图 GoodsMatting), 460/106751 (人像抠图 AIPortraitMatting).
All four are COS-hosted synchronous image operations with the same wire
form; they differ in `ci-process` and in one param each. The op resolves as:

    ctx.mapped_model  >  X-Channel-Options.op  >  the client's raw `model`
    (only when it names a known op verbatim)  >  none -> loud 400.

No silent default: a request that names no operation must not be answered by
whichever op happens to be first in a table.

Shape of the upstream
---------------------
A COS-hosted image operation is not a JSON API: the same URL answers with
the processed image bytes directly. Of the processing modes the references
document, this script speaks **下载时处理 via `detect-url`** -- the one mode
where the source can be any public URL and the result comes back in the
response body instead of being written into the bucket:

    GET /?ci-process=<op>&detect-url=<urlencoded>&<op-specific params>
    Host: <BucketName-APPID>.cos.<Region>.myqcloud.com
    Authorization: q-sign-algorithm=sha1&q-ak=...&q-signature=...

The call is synchronous: one request, one image out. The upstream has no
prompt and no n -- canonical fields that make no sense here are ignored, and
the ones that matter are:

    image (exactly one)  -> detect-url. A URL rides through untouched (the
                            vendor remains the only fetcher that matters, but
                            a probe copy is downloaded here for the preflight
                            -- 20MB cap, TTL cache); a data URI or bare
                            base64 is re-hosted to our object storage first,
                            because COS cannot be handed an inline payload.
                            Storage absent -> a loud 503, never a data URI in
                            that slot.
    preflight + convert  -> dimensions and format are read before anything is
                            sent. PNG/JPEG only; shorter edge >= 32 (smaller
                            fails here with the real numbers -- the vendor's
                            own wording for this is a misleading
                            ImageTooLarge, whose XML detail the engine drops).
                            An input over the upstream cap (1920 for 超分,
                            7680 long edge for 抠图) is **downscaled to fit
                            automatically** -- the caller never has to
                            pre-shrink. The 抠图 4320 edge is deliberately
                            not enforced: a portrait 8K must not be misjudged
                            by a literal reading.
    size                 -> the desired OUTPUT edge, super-res only. Tiers
                            "1k/2k/4k/8k" (1024/2048/4096/8192) or "WxH"
                            (long edge). The script picks the magnify in
                            {1,2,4} whose product lands closest to the
                            target (ties -> smaller; an input already at or
                            past the target gets m=1, clarity-only), after
                            any downsizing. An explicit body `magnify` wins
                            over the size-derived one; `X-Channel-Options.
                            magnify` only fills the gap when neither is
                            given.

    AISuperResolution:
      magnify           -> magnify, {1, 2, 4}, default 2. 1 means clarity
                           enhancement at unchanged resolution. Body wins
                           over the channel option; nonsense fails 400.
    the three mattings:
      center-layout     -> center-layout, {0, 1}, default 0 (subject centred).
      padding-layout    -> padding-layout, "<dx>x<dy>", each side <= 1000 px.
                           Passed through after validation.

A param its operation has no field for is dropped (magnify on a matting,
layout on the super-res), not forwarded to an upstream that would only
complain about it. On the canonical door these arrive as vendor extras; the
chat/responses/edits doors fold them away, where the channel options fill
the gap.

COS fetches the detect-url itself, so a URL behind auth or a firewall is the
caller's problem -- the same trade every URL-carrying reference makes.

Signature
---------
COS XML API request signature (doc 436/7778), mirroring the vendor SDK
(cos-python-sdk-v5 ``cos_auth.py``) step for step:

    SignKey      = Hex(HMAC-SHA1(SecretKey, KeyTime))
    HttpString   = lower(method) \n pathname \n sorted encoded k=v params
                   \n sorted encoded k=v headers \n
                   (UrlEncode = quote, safe='-_.~'; header names lowercased)
    StringToSign = "sha1\n" KeyTime "\n" Hex(SHA1(HttpString)) "\n"
    Signature    = Hex(HMAC-SHA1(SignKey, StringToSign))

KeyTime is backdated 60 s (clock skew, as the SDK does) and valid 900 s.
Only `host` is signed; the query rides listed, and the full URL -- query
included -- is built here and emitted whole. The framework's urlencode would
pick quote_plus, whose space handling differs from the signature's; a
signature naming bytes the wire does not carry is a 403 waiting for a slow
day, so the two are pinned together byte for byte instead.

Response
--------
A 200 answer IS the image (Content-Type image/png). Mapping back:

    url (default)  -> the bytes re-hosted via ctx.upload_temp_image (minio:
                      presigned GET, TEMP_IMAGE_TTL). Storage absent or failed
                      -> the item degrades to b64_json -- never a data URI in
                      the url slot, the house rule.
    b64_json       -> the bytes, base64-encoded.
    width/height   -> ride beside the payload when Pillow can read them.

A 200 body that is neither bytes nor a recognisable image fails loudly (502
upstream_error). COS errors are 4xx/5xx with an XML body: the engine's
raise_for_status turns them into an UpstreamError before the response phase
runs, so the script never sees them -- the vendor's Code/Message text is not
surfaced (a framework gap, noted here rather than worked around).

`response_format` is honoured by conversion; through the edits/chat/responses
doors the field is folded away and the default (url) applies.
"""

import hashlib
import hmac
import re
import time
from urllib.parse import quote, urlparse

#: The four operations this script speaks, as the vendor spells them in
#: `ci-process`. Anything else is refused before a socket opens.
KNOWN_OPS = frozenset(
    {"AISuperResolution", "AIPicMatting", "GoodsMatting", "AIPortraitMatting"}
)
MATTING_OPS = frozenset({"AIPicMatting", "GoodsMatting", "AIPortraitMatting"})

#: 放大倍数。1 = 只做清晰度增强、不改分辨率（460/83793）。
MAGNIFY_VALUES = frozenset({1, 2, 4})
DEFAULT_MAGNIFY = 2

#: 抠图留白 `padding-layout` = "<dx>x<dy>"，单边最大 1000 像素（460/106750）。
_PADDING_RE = re.compile(r"^(\d{1,4})x(\d{1,4})$")
MAX_PADDING = 1000

#: 输入预检边界（460/36620，2026-10-08 实测吻合）。出界上游报的
#: `ImageTooLarge` 措辞误导（4x4 也报 TooLarge），且其 XML 详情在引擎的
#: raise_for_status 处丢失——预检在这里把边界讲成人话。超上限的输入不拒，
#: **自动等比缩到 ≤ 上限**（调用方无需预处理）；拒的只有修不了的：
#: 短边 <32、非 PNG/JPEG。抠图的 4320 边不预检：竖版 8K（4320×7680）
#: 按字面读会被误伤，边界模糊的让上游裁决。
MIN_EDGE = 32
SUPERRES_MAX_EDGE = 1920
MATTING_MAX_EDGE = 7680
ALLOWED_FORMATS = frozenset({"PNG", "JPEG"})

#: `size` 的两套拼法：档位词（不区分大小写）与 "WxH"（取长边）。
SIZE_TIERS = {"1k": 1024, "2k": 2048, "4k": 4096, "8k": 8192}
_SIZE_WXH_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$")

#: KeyTime = [now - SKEW, now + SIGN_EXPIRES]，前者是 SDK 同款的时钟回拨容忍。
SKEW = 60
SIGN_EXPIRES = 900

#: The output shape the caller asked for, parked between the phases -- the
#: response phase cannot see the client's request (same pattern as
#: `tencent_hunyuan/images@v1._STATE`).
_STATE: dict[str, str] = {}


def _enc(value) -> str:
    """The SDK's UrlEncode: quote with ``-_.~`` left bare, everything else %XX."""
    return quote(str(value), safe="-_.~")


def _cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    pathname: str,
    params: dict[str, str],
    headers: dict[str, str],
    now: int | None = None,
) -> str:
    """One COS XML API Authorization value (doc 436/7778, SDK-faithful).

    Params and headers arrive with their **wire values**; the encoding here is
    the same ``_enc`` the wire query is built from, so what is signed is what
    is sent.
    """
    moment = int(time.time()) if now is None else now
    key_time = f"{moment - SKEW};{moment + SIGN_EXPIRES}"

    encoded_params = {_enc(k).lower(): _enc(v) for k, v in params.items()}
    encoded_headers = {_enc(k).lower(): _enc(v) for k, v in headers.items()}
    param_str = "&".join(f"{k}={v}" for k, v in sorted(encoded_params.items()))
    header_str = "&".join(f"{k}={v}" for k, v in sorted(encoded_headers.items()))

    http_string = f"{method.lower()}\n{pathname}\n{param_str}\n{header_str}\n"
    sign_key = hmac.new(
        secret_key.encode(), key_time.encode(), hashlib.sha1
    ).hexdigest()
    string_to_sign = (
        f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    )
    signature = hmac.new(
        sign_key.encode(), string_to_sign.encode(), hashlib.sha1
    ).hexdigest()

    return (
        "q-sign-algorithm=sha1"
        f"&q-ak={_enc(secret_id)}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list={';'.join(sorted(encoded_headers))}"
        f"&q-url-param-list={';'.join(sorted(encoded_params))}"
        f"&q-signature={signature}"
    )


def _resolve_op(ctx, payload) -> str:
    """mapped model > channel option > a raw model that names an op > fail."""
    op = ctx.mapped_model
    if not (isinstance(op, str) and op in KNOWN_OPS):
        op = ctx.options.get("op")
    if not (isinstance(op, str) and op in KNOWN_OPS):
        raw = payload.get("model")
        op = raw if isinstance(raw, str) and raw in KNOWN_OPS else None
    if op is None:
        ctx.fail(
            "该渠道需要一个数据万象操作名：通过 X-Model-Map 映射到 "
            f"{sorted(KNOWN_OPS)} 之一，或 X-Channel-Options.op，或让 model "
            "直接使用操作名",
            code="channel_config_error",
        )
    return op


def _int_field(ctx, value, name, allowed, default):
    """Body > option > default, bool-refused; nonsense fails by name."""
    if value is None:
        value = default
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        ctx.fail(f"{name} 仅支持 {sorted(allowed)}，收到 {value!r}", param=name)
    if value not in allowed:
        ctx.fail(f"{name} 仅支持 {sorted(allowed)}，收到 {value!r}", param=name)
    return value


def _size_target(ctx, payload) -> int | None:
    """`size` = 期望的输出边长（超分专属）。"1k/2k/4k/8k" 或 "WxH"（长边）。

    body > 渠道选项；没给就是 None（magnify 走原优先级）。拼不对是调用方
    的明确笔误，响亮地拒，而不是猜一个档位。
    """
    value = payload.get("size")
    if value is None:
        value = ctx.options.get("size")
    if value is None:
        return None
    if not isinstance(value, str):
        ctx.fail("size 须为 1k/2k/4k/8k 或 WxH（如 2048x2048）", param="size")
    text = value.strip().lower()
    if text in SIZE_TIERS:
        return SIZE_TIERS[text]
    match = _SIZE_WXH_RE.match(text)
    if match is None:
        ctx.fail("size 须为 1k/2k/4k/8k 或 WxH（如 2048x2048）", param="size")
    return max(int(match.group(1)), int(match.group(2)))


def _magnify_of(ctx, payload, input_edge: int) -> int:
    """显式 body magnify > size 推导 > 渠道选项 > 默认 2。

    size 推导＝选 {1,2,4} 中「输入边长 × m」离目标最近的档（并列取小）。
    输入已达或超过目标时 m=1 自然胜出——只做清晰度增强，不再放大。
    """
    if payload.get("magnify") is not None:
        return _int_field(
            ctx, payload.get("magnify"), "magnify", MAGNIFY_VALUES,
            payload.get("magnify"),
        )
    target = _size_target(ctx, payload)
    if target is not None:
        return min(MAGNIFY_VALUES, key=lambda m: (abs(input_edge * m - target), m))
    return _int_field(
        ctx, None, "magnify", MAGNIFY_VALUES,
        ctx.options.get("magnify", DEFAULT_MAGNIFY),
    )


def _preflight(ctx, op, info) -> None:
    """格式与短边下限。超上限的不在这里拒——交给自动缩图。"""
    fmt = str(info.get("format") or "").upper()
    if fmt not in ALLOWED_FORMATS:
        ctx.fail(
            f"该渠道仅支持 PNG/JPEG 输入，收到 {fmt or '未知格式'}",
            param="image",
        )
    width, height = info.get("width"), info.get("height")
    if not isinstance(width, int) or isinstance(width, bool):
        return  # 尺寸读不出：交给上游裁决
    if not isinstance(height, int) or isinstance(height, bool):
        return
    if min(width, height) < MIN_EDGE:
        ctx.fail(
            f"{op} 输入最小 {MIN_EDGE}×{MIN_EDGE}，收到 {width}×{height}"
            "（再小上游会报误导性的 ImageTooLarge）",
            param="image",
        )


def _padding_of(ctx, payload) -> str | None:
    """`padding-layout` = "<dx>x<dy>", each side <= 1000 px, default none.

    Validated, not rewritten: the value is forwarded exactly as it will ride
    the query, so a caller reading its own request back sees what COS saw.
    """
    value = payload.get("padding-layout")
    if value is None:
        value = ctx.options.get("padding-layout")
    if value is None:
        return None
    value = str(value).strip().replace(" ", "")
    match = _PADDING_RE.match(value)
    if match is None or int(match.group(1)) > MAX_PADDING or int(match.group(2)) > MAX_PADDING:
        ctx.fail(
            f"padding-layout 须为 <dx>x<dy> 且 dx/dy ≤ {MAX_PADDING}，"
            f"收到 {value!r}",
            param="padding-layout",
        )
    return value


def _credentials(ctx) -> tuple[str, str]:
    """Splits the channel key into (SecretId, SecretKey), loudly or not at all."""
    secret_id, sep, secret_key = (ctx.key or "").partition("|")
    if not sep or not secret_id.strip() or not secret_key.strip():
        ctx.fail(
            "该渠道的 Authorization 须为 <SecretId>|<SecretKey>（腾讯云 API 密钥，"
            "竖线连接）；当前值拆不开",
            code="channel_config_error",
        )
    return secret_id.strip(), secret_key.strip()


async def _response(ctx, payload):
    """Image bytes in hand -> the canonical single-item envelope."""
    want = _STATE.pop(ctx.request_id, None)

    if not isinstance(payload, (bytes, bytearray)):
        ctx.fail(
            "COS returned no image bytes for the super-resolution call",
            code="upstream_error",
            status=502,
            err_type="server_error",
        )
    payload = bytes(payload)

    mime = ctx.sniff_mime(payload)
    if mime == "application/octet-stream":
        # The same sentinel the framework's image gate uses: a 200 body with
        # no recognisable image magic is this channel's quiet way of failing,
        # so it is made loud here.
        ctx.fail(
            "COS answered 200 but the body is not an image",
            code="upstream_error",
            status=502,
            err_type="server_error",
        )
    ext = mime.split("/", 1)[1] if mime.startswith("image/") else "png"

    if want == "b64_json":
        item = {"b64_json": ctx.encode_b64(payload)}
    else:
        # Default (and `url`): a link of ours. The upstream offers none of its
        # own, so the bytes are stored here -- and a store that degraded to a
        # data URI is answered with b64_json instead, never a data URI dressed
        # up as a url.
        stored = await ctx.upload_temp_image(payload, ext=ext)
        if ctx.is_url(stored):
            item = {"url": stored}
        else:
            item = {"b64_json": ctx.encode_b64(payload)}

    info = await ctx.image.info(payload)
    for key in ("width", "height"):
        value = info.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            item[key] = value

    return {"data": [item]}


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = payload.get("response_format")
    if requested == "url" or requested == "b64_json":
        _STATE[ctx.request_id] = requested

    secret_id, secret_key = _credentials(ctx)
    op = _resolve_op(ctx, payload)

    image = payload.get("image")
    refs = image if isinstance(image, list) else ([image] if image else [])
    if not refs:
        ctx.fail(
            "该渠道处理已有图片（超分/抠图都需要一张输入图，image 字段）",
            param="image",
        )
    if len(refs) > 1:
        ctx.fail(
            "该渠道一次只处理一张图（上游 detect-url 单图语义）",
            param="image",
        )

    ref = refs[0]

    # 预检需要字节：URL 先下载一份（20MB 上限 + TTL 缓存），内联解码一次。
    data = await ctx.image_bytes(ref)
    info = await ctx.image.info(data)
    _preflight(ctx, op, info)

    # `size` 的另一半语义：输入长边已达目标 ⇒ 超分给不出更好的结果
    # （它只会从已有像素放大），原图直出——不发上游、零计费。显式
    # `magnify` 不吃这条捷径：那是「真跑一次增强」的明确指令，超限照旧缩图送。
    if op == "AISuperResolution" and payload.get("magnify") is None:
        target = _size_target(ctx, payload)
        if target is not None and max(
            int(info["width"]), int(info["height"])
        ) >= target:
            ctx.emit(local_result=data)
            return {}

    # 超上游上限的输入自动等比缩到 ≤ 上限（调用方无需预处理）。缩过的字节
    # 必然要转存；URL 直通只属于「原样送达」的输入。
    max_edge = SUPERRES_MAX_EDGE if op == "AISuperResolution" else MATTING_MAX_EDGE
    width, height = int(info["width"]), int(info["height"])
    resized = False
    if max(width, height) > max_edge:
        scale = max_edge / max(width, height)
        data = await ctx.image.resize(
            data, round(width * scale), round(height * scale)
        )
        info = await ctx.image.info(data)
        width, height = int(info["width"]), int(info["height"])
        resized = True

    if ctx.is_url(ref) and not resized:
        source = ref.strip()
    else:
        fmt = str(info.get("format") or "").upper()
        source = await ctx.upload_temp_image(
            data, ext="jpeg" if fmt == "JPEG" else "png"
        )
        if not ctx.is_url(source):
            ctx.fail(
                "输入图不是公网可取的 URL，且对象存储不可用（无法中转上传给上游处理）",
                code="storage_unavailable",
                status=503,
                err_type="server_error",
                param="image",
            )

    params = {"ci-process": op, "detect-url": source}
    if op == "AISuperResolution":
        params["magnify"] = str(_magnify_of(ctx, payload, max(width, height)))
    else:
        # A matting's two layout knobs. `center-layout` arrives as 0/1 (or
        # "0"/"1" from hand-written options); the repaired int rides as its
        # canonical spelling.
        center = _int_field(
            ctx, payload.get("center-layout"), "center-layout", {0, 1},
            ctx.options.get("center-layout", 0),
        )
        params["center-layout"] = str(center)
        padding = _padding_of(ctx, payload)
        if padding is not None:
            params["padding-layout"] = padding

    parsed = urlparse(ctx.upstream_url)
    netloc = parsed.netloc.lower()
    pathname = parsed.path or "/"
    headers = {"host": netloc}

    # The wire query and the signed param string are the same sorted, encoded
    # pairs -- see the Signature note in the module docstring.
    encoded = {_enc(k).lower(): _enc(v) for k, v in params.items()}
    query = "&".join(f"{k}={v}" for k, v in sorted(encoded.items()))
    url = f"{parsed.scheme.lower()}://{netloc}{pathname}?{query}"

    ctx.emit(
        url=url,
        method="GET",
        headers={
            "Authorization": _cos_authorization(
                secret_id, secret_key, "GET", pathname, params, headers
            ),
            "Host": netloc,
        },
    )
    # The URL carries everything; an empty dict keeps the engine from turning
    # a body into query parameters or JSON on a GET.
    return {}
