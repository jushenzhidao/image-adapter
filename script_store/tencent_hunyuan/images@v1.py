"""tencent_hunyuan/images@v1: Hunyuan Image 3.5 (TokenHub), chat-shaped and sync.

Channel setup (New API side):
  X-Upstream-Url:    https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v35-generation
  X-Script-Ref:      tencent_hunyuan/images@v1
  Authorization:     Bearer <TOKENHUB_API_KEY>
  X-Channel-Options: {"model": "hy-image-v3.5-preview"}  (optional)
  X-Model-Map:       gpt-image-2=hy-image-v3.5-preview   (optional, > option > default)

Shape of the upstream
---------------------
TokenHub exposes Hunyuan Image 3.5 behind a chat-shaped body -- `messages`
carrying `text` and `image_url` content parts -- but the call is **synchronous**:
one POST returns the final frame, no task id, no polling (docs
cloud.tencent.com/document/product/1823/135745; field behaviour re-measured
live on 2026-09-30, see the "Measured" block below and
reports/2026-09-30_tencent-hunyuan-probe/). The canonical body is
therefore flattened into exactly one `user` message, which is also what the
adapter's multi-turn rule already means: the last `user` turn is the
instruction, everything before it is context this integration does not carry.

    canonical                    -> hunyuan
    prompt                       -> content[-1].type=text
    image (str | list[str])      -> content[..].type=image_url, in input order,
                                    before the text part
    size  ("WxH")                -> size, same spelling upstream takes
    seed, session, footnote,     -> forwarded when present
    generate_max_pixels,
    resize_max_pixels,
    use_search_tool
    n, quality, style, mask      -> dropped (upstream has no such fields; one
                                    call answers one image)

Measured (2026-09-30, live upstream, 6 requests -- see
reports/2026-09-30_tencent-hunyuan-probe/):

  * `size` is honoured exactly ("1024x1024" -> a 1024x1024 image). **An
    out-of-range size is clamped, not refused**: 100x100 came back as a
    256x256 image with a 200. The bounds in the vendor's reference are a
    clamp table, so validation stays the vendor's business and this script
    keeps forwarding whatever it was given.
  * Without `size` the model picks its own canvas: 1536x1536 (the 1.5K
    default area) for square prompts, 1248x1872 for one it read as
    portrait.
  * The final frame carried **no `usage` object on any request**, although
    the reference documents `usage.total_tokens`. The forwarding branch
    below is kept (guarded on the field actually being present), so a
    vendor that starts sending it flows through with no script change.
  * `data:image/...;base64` and bare base64 (repaired to a data URI by
    `_wire_ref`) are both accepted as reference carriers; round trips ran
    7-19 s per image.

Reference wire form
-------------------
The upstream accepts an http(s) URL or a `data:` URI per reference, at most
20 MB each and 20 references. The default path is therefore the cheap one: a
URL is forwarded verbatim and fetched by nobody but the vendor, a data URI
rides through as it is (this is what the `edits` front door produces from an
upload). The one gap is **bare base64**, which the reference does not list:
a value that is neither a URL nor a data URI gets a `data:` prefix built from
the sniffed mime type -- a repair, not a rewrite, and one that degrades to
passing the value through when its bytes do not decode. Unlike ARK this
channel documents no vendor-side fetch timeout, so there is no `image_ref_mode`
and no retry-on-fetch-failure machinery: nothing measured demands them.

Response
--------
The final frame hides the picture at `choices[0].delta.image.url` -- a signed
COS link that expires in **12 hours** -- with width/height beside it, and may
instead carry an `error` object on a failed turn. The mapping back:

    url present   -> {"created", "data": [{"url", "width"?, "height"?}], "usage"}
    error object  -> ctx.fail(upstream message, 502 upstream_error)
    no picture    -> ctx.fail(..., 502 upstream_error)  -- a 200 without an
                     image is this channel's quiet way of failing, so it is
                     made loud here
    refusal       -> content_filter 400 via ctx.matched_moderation (the shared
                     wordings; no vendor code has been measured yet)

`response_format` is honoured by conversion, because the upstream answers with
a link no matter what was asked: `b64_json` downloads the link and encodes it;
`url` passes the vendor's link through, or -- with the cross-channel
`rehost_url: true` option -- swaps it for one of ours via `ctx.rehost_image`.
Given a 12-hour expiry that option earns its keep here more than on most
channels, and it stays off by default like everywhere else.

Watermark
---------
The vendor's reference describes the delivered image as carrying a mark, and
the only knob is `footnote` -- custom text, not an off switch. There is
therefore no default to send: the field is forwarded only when the caller or
the channel options name it, and a caller wanting a clean image is told by
this paragraph that this upstream offers no way to omit the mark entirely.

`session` is forwarded when given: the vendor uses it for consistent-hash
scheduling across multi-turn edits. This script never invents one -- the
adapter's model is one request, one result -- so a caller that sends no
session keeps a stateless channel.
"""

from functools import partial

DEFAULT_MODEL = "hy-image-v3.5-preview"

#: Vendor fields forwarded when present: the request body wins over the channel
#: option, and silence forwards nothing (there is no third layer -- the
#: upstream's own defaults apply to whatever this does not send).
VENDOR_FIELDS = (
    "seed",
    "session",
    "footnote",
    "generate_max_pixels",
    "resize_max_pixels",
    "use_search_tool",
)

#: The output shape the caller asked for, parked between the phases -- the
#: response phase cannot see the client's request (same pattern as
#: `openai/images@v1._STATE`).
_STATE: dict[str, str] = {}


def _wire_ref(ctx, ref):
    """One client reference in the form the upstream accepts.

    URLs and data URIs ride through untouched; anything else is treated as
    bare base64 and given the `data:` prefix the reference names -- with a
    fallback to the original value when the bytes do not decode, because a
    string that is neither of the three shapes is the caller's mistake to
    see from the vendor, not one worth turning into a 500 here.
    """
    ref = ref.strip()
    if ctx.is_url(ref) or ctx.is_data_uri(ref):
        return ref
    try:
        mime = ctx.sniff_mime(ctx.decode_b64(ref))
    except Exception:  # noqa: BLE001 - pass the odd value through, upstream rules
        return ref
    return f"data:{mime};base64,{ref}"


async def _content_item(ctx, ref):
    """One reference as an `image_url` content part (fan-out worker)."""
    return {"type": "image_url", "image_url": {"url": _wire_ref(ctx, ref)}}


def _delta_image(payload):
    """(url, width, height) out of the final frame, or (None, None, None)."""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, None, None
    first = choices[0]
    if not isinstance(first, dict):
        return None, None, None
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None, None, None
    image = delta.get("image")
    if not isinstance(image, dict):
        return None, None, None
    return image.get("url"), image.get("width"), image.get("height")


def _frame_error(payload):
    """The frame's `error` object when it is shaped like one."""
    error = payload.get("error")
    return error if isinstance(error, dict) else None


async def _response(ctx, payload):
    if not isinstance(payload, dict):
        return payload

    # A refusal first, before anything else reads the frame: the shared
    # wordings catch a vendor that answers 200 with Chinese refusal text in
    # its error message. No vendor-specific code is matched -- none has been
    # measured, and inventing one is how a retryable hiccup gets mislabelled.
    token = ctx.matched_moderation(payload)
    if token:
        ctx.fail_moderation(token)

    error = _frame_error(payload)
    if error is not None:
        _STATE.pop(ctx.request_id, None)
        message = error.get("message") or "Hunyuan image upstream returned an error"
        ctx.fail(str(message), code="upstream_error", status=502)

    url, width, height = _delta_image(payload)
    if not isinstance(url, str) or not url:
        _STATE.pop(ctx.request_id, None)
        ctx.fail(
            "Hunyuan returned no image in its final frame",
            code="upstream_error",
            status=502,
        )

    item = {"url": url}
    if isinstance(width, int) and not isinstance(width, bool):
        item["width"] = width
    if isinstance(height, int) and not isinstance(height, bool):
        item["height"] = height

    want = _STATE.pop(ctx.request_id, None)
    if want == "b64_json":
        # The upstream only ever answers with a link, so honouring the shape
        # means the fetch happens here. A dead or non-picture link fails the
        # request through the checked downloader rather than passing as a
        # picture-less success.
        item.pop("url")
        item["b64_json"] = ctx.encode_b64(await ctx.download_image(url))
    elif ctx.options.get("rehost_url") is True and ctx.is_url(url):
        # The vendor's link dies in 12 hours; ours does not. `None` -- no
        # object storage behind the channel -- keeps the vendor's link, the
        # same degradation every other script accepts.
        stored = await ctx.rehost_image(url)
        if stored is not None:
            item["url"] = stored

    out = {"created": payload.get("created", 0), "data": [item]}
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage.get("total_tokens") is not None:
        out["usage"] = {"total_tokens": usage["total_tokens"]}
    return out


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = payload.get("response_format")
    if requested == "url" or requested == "b64_json":
        _STATE[ctx.request_id] = requested

    prompt = str(payload.get("prompt") or "").strip()
    image = payload.get("image")
    refs = image if isinstance(image, list) else ([image] if image else [])

    # The vendor takes the last `user` turn as the instruction; this channel
    # carries exactly one, whose image parts precede the text part.
    content = []
    if refs:
        content.extend(await ctx.fanout(refs, partial(_content_item, ctx)))
    if prompt:
        content.append({"type": "text", "text": prompt})

    body = {
        "model": ctx.mapped_model or ctx.options.get("model", DEFAULT_MODEL),
        "messages": [{"role": "user", "content": content}],
    }
    # `size` uses the same "WxH" spelling on both sides, so it is forwarded as
    # it arrived. Measured: the vendor clamps an out-of-range value to its
    # bounds (100x100 -> a 256x256 image, HTTP 200) rather than refusing, so
    # there is nothing to pre-validate here and nothing that could drift.
    size = payload.get("size")
    if isinstance(size, str) and size.strip():
        body["size"] = size.strip()
    for key in VENDOR_FIELDS:
        value = payload.get(key, ctx.options.get(key))
        if value is not None:
            body[key] = value
    return body
