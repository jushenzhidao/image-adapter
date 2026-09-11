"""google/images@v1: Gemini Image ("Nano Banana") through generateContent.

Channel setup (New API side):
  X-Upstream-Url:    https://generativelanguage.googleapis.com/v1beta/models/
                     gemini-3-pro-image:generateContent
  X-Script-Ref:      google/images@v1
  Authorization:     Bearer <GEMINI_API_KEY>
  X-Auth-Emit:       header:x-goog-api-key     (bare key, no prefix)
  X-Channel-Options: {"default_response_format": "b64_json",
                      "image_ref_mode": "auto",            auto|url|inline
                      "inline_max_bytes": 4194304,         per image
                      "inline_total_max_bytes": 6291456,   per request
                      "default_mime_type": "image/png",
                      "client_url_passthrough": true,
                      "image_config_style": "auto",        imageConfig|responseFormat
                      "max_input_images": 10,
                      "image_text_order": "text_first"}    text_first|text_last

This vendor differs from the OpenAI-shaped ones in four ways the script absorbs:

1. The model is a path segment, not a body field, so the client's `model` is
   written back into the URL (and emit(url=...) re-runs the SSRF check, which is
   why the upstream host must be in UPSTREAM_HOST_ALLOWLIST when that is set).
2. Input accepts three shapes and the upstream can fetch URLs itself: a client
   URL is forwarded as file_data.file_uri with no download and no base64
   inflation; encoded input is inlined until it outgrows the caps and is then
   re-hosted as a URL. See _ref_part.
3. Output is base64 only (`inlineData`), so `response_format: "url"` means
   uploading to MinIO here. TEMP_IMAGE_TTL applies (1h by default), not OpenAI's
   24h. With no MinIO configured the upload degrades to a data URI, which is not
   a link and is never returned as one: that item falls back to `b64_json`
   instead of failing, matching openai/images@v1.
4. The response phase cannot see the client's request body, so
   `response_format` travels through a module-level table keyed by request_id.

A 200 is not success upstream: safety blocks, refusals and "no image" all arrive
as a normal response with no image parts. Those are turned into client-visible
errors via ctx.fail() (adapter/ctxapi/fault.py) instead of a 500 or a silent
empty result.

CONTRACT STATUS: verified 2026-09-11 against a live gateway
(api.chatfire.cn, model gemini-3.1-flash-image-preview), together with Google's
published docs:
  * parts must be camelCase (fileData/fileUri/mimeType). That gateway drops the
    snake_case spellings as unknown fields, and the upstream then answers 500 with
    "contents[0].parts[1].data: required oneof field 'data' must have one
    initialized field". inline_data is more forgiving, but everything outbound is
    camelCase for consistency; the reply side still reads both.
  * `imageConfig` and `responseFormat.image` are BOTH accepted there, and 1K / 2K
    / 4K all work (a 4K reply measured 10.7MB of base64). image_config_style still
    matters for gateways that ship only one generation of the field.
  * replies came back as image/jpeg, and an 8.27MB inline_data was accepted -- so
    "7MB per image" is a floor for that gateway, not the ceiling. The default
    inline_max_bytes stays conservative because the weakest upstream sets the bar.

STILL UNVERIFIED: per-model imageSize support in MODELS (only the 3.1 flash
preview was exercised), the inline ceiling above 8.27MB, mime/type mismatch
tolerance, and the exact shape of a safety block.
"""

from urllib.parse import urlsplit, urlunsplit

# Model facts -- aliases, resolution tiers, which ratios a model accepts -- live in
# script_store/capabilities/google.json and arrive through ctx.caps(). They used to be
# defined here, which meant every calibration (a new tier; a gateway that decorates
# model ids with "-preview") was a script edit, a manifest re-hash and a release.
# Reading them from data costs nothing and keeps one copy of the truth.
#
# ctx.caps() returning None means "no facts for this model": this script then sends
# the minimum (prompt + parts + modalities) and lets the upstream pick its own
# resolution, rather than guessing from a table it carries itself.

#: Pixel edge -> the tier worth asking for. These thresholds are *calibrated*, not
#: derivable: an edge of 768 counts as the 512 tier because anything below 1K is
#: already a downgrade the family will not honour. Which tiers exist, and how to
#: choose among them, moved to ctx.fit_tier + capabilities/google.json; what stays
#: here is the only part that is our own judgement.
_TIER_CEILING = ((768, 512), (1024, 1024), (2048, 2048))


def _want_tier(edge):
    """Long edge -> the tier value to ask for (the ceiling, not the exact size)."""
    for limit, value in _TIER_CEILING:
        if edge <= limit:
            return value
    return 4096


_EXT_MIME = (
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".webp", "image/webp"),
)

#: Finish reasons that mean "the model refused", as opposed to "it broke".
FILTERED = frozenset(
    {
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
        "PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "RECITATION",
        "SAFETY",
    }
)

#: request_id -> {"response_format": str}. The response phase receives the
#: upstream reply, not the client body, so anything the client asked for that
#: only affects how the reply is shaped has to be carried across. Capped in
#: _remember so a request that never reaches its response phase cannot leak.
_STATE: dict[str, dict] = {}
_STATE_MAX = 512


def _model_url(url, model):
    """Rewrites the model segment of a generateContent URL, keeping the rest."""
    parts = urlsplit(url)
    head, sep, _ = parts.path.rpartition("/models/")
    if not sep:
        return url
    return urlunsplit(parts._replace(path=f"{head}/models/{model}:generateContent"))


def _image_config(style, ratio, size):
    """The aspect/size block, in whichever generation of the field the API wants.

    Returns None when there is nothing to say -- which happens when the table has
    no ratio list and the model fixes its own resolution. Sending an empty block
    would be a claim we cannot back, so the caller skips it instead.
    """
    inner = {}
    if ratio:
        inner["aspectRatio"] = ratio
    if size is not None:
        inner["imageSize"] = size
    if not inner:
        return None
    if style == "responseFormat":
        return {"responseFormat": {"image": inner}}
    return {"imageConfig": inner}


def _b64_size(ref):
    """Decoded size of an encoded reference, from its length -- without decoding.

    Padding makes this a slight over-estimate (4 chars -> exactly 3 bytes), which
    errs towards re-hosting: the safe direction whenever a cap is in play.
    """
    if ref.startswith("data:"):
        _, _, ref = ref.partition(",")
    return len(ref) * 3 // 4


def _data_uri_mime(ref):
    """The mime a data URI already carries -- cheaper than decoding to sniff one."""
    return ref[5:].split(";", 1)[0] or "image/png"


def _declared_mime(ctx, ref):
    """A file_uri needs a mime_type, and we deliberately avoid downloading to sniff."""
    path = urlsplit(ref).path.lower()
    for ext, mime in _EXT_MIME:
        if path.endswith(ext):
            return mime
    return str(ctx.options.get("default_mime_type", "image/png"))


def _uri_part(ref, mime):
    # camelCase keys: verified 2026-09-11 against api.chatfire.cn, which drops the
    # snake_case spellings as unknown fields -- the upstream then answers 500 with
    # "contents[0].parts[1].data: required oneof field 'data' must have one
    # initialized field". camelCase is the protobuf JSON mapping's canonical name,
    # so it is the safer of the two everywhere.
    return {"fileData": {"mimeType": mime, "fileUri": ref.strip()}}


def _passthrough(ctx):
    return ctx.options.get("client_url_passthrough", True) is not False


def _remember(ctx, response_format):
    if len(_STATE) > _STATE_MAX:
        for key in list(_STATE)[:_STATE_MAX // 2]:
            _STATE.pop(key, None)
    _STATE[ctx.request_id] = {"response_format": response_format}


async def _hosted_part(ctx, ref, mime, raw):
    """Re-host bytes and hand the upstream a URL -- the way past the inline cap."""
    url = await ctx.upload_temp_image(raw, ext=mime.split("/", 1)[-1] or "png")
    if url.startswith("data:"):
        # upload_temp_image degrades to a data URI when storage is off. For an
        # image this large that is not a usable answer, so fail loudly: the
        # alternative is an upstream 400 the caller cannot explain.
        ctx.fail(
            "Image is too large for inline parts and object storage is unavailable",
            param="image",
            code="image_too_large",
            status=413,
        )
    return _uri_part(url, mime)


async def _bytes_part(ctx, ref, data, mime, mode, allowance):
    """Bytes in hand: hand the upstream a URL, or inline them if they fit."""
    per_image = int(ctx.options.get("inline_max_bytes", 4 * 1024 * 1024))
    over = mode == "url" or (
        mode == "auto" and (len(data) > per_image or len(data) > allowance[0])
    )
    if over:
        return await _hosted_part(ctx, ref, mime, data)
    if mode == "auto":
        # Only auto mode meters the request-wide budget; "inline" is unconditional
        # by definition, so mixing shapes there would just make it unpredictable.
        allowance[0] -= len(data)
    return {"inlineData": {"mimeType": mime, "data": ctx.encode_b64(data)}}


async def _ref_part(ctx, ref, mode, allowance):
    """Three input shapes, four paths. Never fetch or encode what can be avoided.

    `allowance` is a one-element cell holding what is left of this request's
    inline budget: the upstream cap that matters is per request, so several
    individually small images can still overflow it.
    """
    ref = ref.strip()
    per_image = int(ctx.options.get("inline_max_bytes", 4 * 1024 * 1024))

    if ctx.is_url(ref):
        if mode != "inline" and _passthrough(ctx):
            return _uri_part(ref, _declared_mime(ctx, ref))  # zero download
        data = await ctx.image_bytes(ref)  # we must fetch it ourselves
        return await _bytes_part(ctx, ref, data, ctx.sniff_mime(data), mode, allowance)

    fits = mode == "auto" and _b64_size(ref) <= min(per_image, allowance[0])
    if fits and ctx.is_data_uri(ref):
        # Cheapest possible inline: the prefix already carries the mime and
        # image_b64 validates-and-returns the string without re-encoding it.
        blob = await ctx.image_b64(ref)
        allowance[0] -= _b64_size(blob)
        return {"inlineData": {"mimeType": _data_uri_mime(ref), "data": blob}}

    # Bare base64 only reaches here when it fits but carries no mime, so one
    # decode is unavoidable -- and with the bytes in hand, encoding is cheap.
    data = await ctx.image_bytes(ref)
    return await _bytes_part(ctx, ref, data, ctx.sniff_mime(data), mode, allowance)


def _refs_of(payload, ctx):
    image = payload.get("image")
    if image is None:
        return []
    refs = image if isinstance(image, list) else [image]
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            ctx.fail(
                "'image' must be a URL, a data URI or a base64 string", param="image"
            )
    return refs


def _collect_images(payload):
    """Every image the reply carries, as (mime, base64).

    Both spellings of the field are accepted: official REST examples use
    snake_case and at least one production caller sends camelCase.
    """
    found = []
    for candidate in payload.get("candidates") or []:
        parts = (candidate.get("content") or {}).get("parts") or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                found.append((mime, inline["data"]))
    return found


def _refusal_text(payload):
    """What the model said instead of drawing -- worth surfacing to the caller."""
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if part.get("text"):
                return str(part["text"])[:200]
    return ""


def _finish_reason(payload):
    for candidate in payload.get("candidates") or []:
        return candidate.get("finishReason") or candidate.get("finish_reason") or ""
    return ""


def _usage(payload):
    """usageMetadata -> the OpenAI block the control plane bills from.

    Billing counts output tokens, and for these models the image *is* the output,
    so thoughtsTokenCount belongs in it too. Missing fields must not produce a
    null: a control plane reads null as zero and under-bills silently.
    """
    meta = payload.get("usageMetadata") or payload.get("usage_metadata") or {}
    prompt = int(meta.get("promptTokenCount") or 0)
    produced = int(meta.get("candidatesTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    output = produced + thoughts
    images = 0
    for detail in meta.get("candidatesTokensDetails") or []:
        if str(detail.get("modality", "")).upper() == "IMAGE":
            images += int(detail.get("tokenCount") or 0)
    images = images or output
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": int(meta.get("totalTokenCount") or (prompt + output)),
        "input_tokens_details": {"text_tokens": prompt, "image_tokens": 0},
        "output_tokens_details": {
            "image_tokens": images,
            "text_tokens": max(output - images, 0),
        },
    }


async def _response(ctx, payload):
    """Upstream 200 -> the OpenAI envelope, or a client-visible error."""
    images = _collect_images(payload)
    if not images:
        blocked = (payload.get("promptFeedback") or {}).get("blockReason")
        reason = _finish_reason(payload)
        said = _refusal_text(payload)
        detail = f": {said}" if said else ""
        if blocked or reason in FILTERED:
            ctx.fail(
                f"Gemini refused this request ({blocked or reason}){detail}",
                param="prompt",
                code="content_filter",
            )
        if reason == "MAX_TOKENS":
            ctx.fail(
                "Gemini ran out of output tokens before producing an image",
                code="upstream_error",
                status=502,
            )
        where = f" ({reason})" if reason else ""
        ctx.fail(
            f"Gemini returned no image{where}{detail}",
            param="prompt",
            code="no_image_generated",
        )

    state = _STATE.pop(ctx.request_id, None) or {}
    fmt = state.get("response_format") or ctx.options.get(
        "default_response_format", "b64_json"
    )

    data = []
    for mime, blob in images:
        if fmt == "url":
            raw = ctx.decode_b64(blob)
            url = await ctx.upload_temp_image(raw, ext=mime.split("/", 1)[-1] or "png")
            if url.startswith("data:"):
                # Without storage, upload_temp_image degrades to a data URI. That
                # is not a link and must never be dressed up as one, so the
                # upstream's own shape is returned instead: the caller asked for
                # `url` and gets base64, which is a disappointment but a usable
                # answer. Failing outright would punish the caller for a gap in
                # our configuration -- and the same rule now holds in
                # openai/images@v1, so both channels behave alike.
                data.append({"b64_json": blob})
                continue
            data.append({"url": url})
        else:
            data.append({"b64_json": blob})

    out = {"created": payload.get("created", 0), "data": data, "usage": _usage(payload)}
    # Kept verbatim alongside the mapped block so billing can be reconciled
    # against what the vendor actually reported.
    meta = payload.get("usageMetadata") or payload.get("usage_metadata")
    if meta is not None:
        out["gemini_usage"] = meta
    return out


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = payload.get("model") or ctx.options.get("model")
    caps = ctx.caps("google", requested)
    if caps is None:
        # No measured facts for this id. Two cases land here: a model nobody has
        # calibrated, and a deployment that ships no table at all. Both get the same
        # treatment -- send less rather than guess.
        # Note aliases live in the table, so without one the control plane must send
        # a real model id; an id we cannot recognise is refused instead of forwarded.
        fallback = (requested or "").strip()
        if not fallback.startswith("gemini-"):
            ctx.fail(f"Unknown model for this channel: {fallback!r}", param="model")
        caps = {"model": fallback, "tiers": [], "wide": True}
        sized = False
    else:
        sized = True
    model = caps["model"]
    ctx.emit(url=_model_url(ctx.upstream_url, model))

    n = payload.get("n", 1)
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        ctx.fail(
            "This upstream returns one image per request; send 'n' separate requests",
            param="n",
            code="unsupported_parameter",
        )
    if payload.get("mask"):
        # Silently dropping it would edit the whole image while the caller
        # believes only the masked region changed.
        ctx.fail(
            "This upstream has no mask/inpainting parameter",
            param="mask",
            code="unsupported_parameter",
        )

    response_format = payload.get("response_format")
    if response_format not in (None, "url", "b64_json"):
        response_format = None
    _remember(ctx, response_format)

    refs = _refs_of(payload, ctx)
    limit = int(ctx.options.get("max_input_images", 10))
    if len(refs) > limit:
        ctx.fail(f"At most {limit} input images are supported here", param="image")

    style = ctx.options.get("image_config_style", "auto")
    if style == "auto":
        style = "responseFormat" if model.startswith("gemini-3.1") else "imageConfig"

    allowance = [int(ctx.options.get("inline_total_max_bytes", 6 * 1024 * 1024))]
    mode = ctx.options.get("image_ref_mode", "auto")
    ref_parts = [await _ref_part(ctx, ref, mode, allowance) for ref in refs]

    # Search grounding can only run alongside text output, so anything asking for
    # tools needs TEXT in the modalities or the request is refused outright.
    wants_text = bool(payload.get("tools")) or bool(
        ctx.options.get("response_modalities_text")
    )
    config = {
        "responseModalities": ["TEXT", "IMAGE"] if wants_text else ["IMAGE"],
    }
    if sized:
        # Without facts the script sends no aspect/size block at all: we cannot know
        # whether this model even accepts the field, so the upstream picks.
        px = ctx.size_to_px(payload.get("size")) or (1024, 1024)
        allowed = caps.get("ratios") or []
        if not caps.get("wide"):
            # A model that refuses the folded shapes gets a nearer one instead of a
            # request it will reject. Which shapes are "folded" is arithmetic, not a
            # list to maintain (see ctx.is_extreme_ratio).
            allowed = [item for item in allowed if not ctx.is_extreme_ratio(item)]
        block = _image_config(
            style,
            ctx.format_ratio(ctx.fit_ratio(px[0], px[1], allowed)),
            ctx.fit_tier(_want_tier(max(px)), caps["tiers"])[0],
        )
        if block:
            config.update(block)

    text_part = {"text": payload.get("prompt", "")}
    if refs and ctx.options.get("image_text_order") == "text_last":
        parts = ref_parts + [text_part]
    else:
        parts = [text_part] + ref_parts

    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
    for key in ("tools", "safetySettings", "systemInstruction"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    return body
