"""volcengine_ark/images@v5: doubao-seedream text-to-image and image-to-image.

Channel setup (New API side):
  X-Upstream-Url: https://ark.cn-beijing.volces.com/api/v3/images/generations
  X-Script-Ref:   volcengine_ark/images@v5
  Authorization:  Bearer <ARK_API_KEY>
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128"}  (optional)

  Nothing else is required. This version keeps the fast path -- a URL reference
  is forwarded verbatim, so ARK fetches it and we download nothing -- and falls
  back to fetching that reference ourselves when ARK reports it could not. See
  "Answering ARK's own fetch timeout" below.
  Optional, to skip the fast path entirely and always inline:
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128",
                      "image_ref_mode": "data_uri"}   ("inline" also accepted)

Seedream exposes editing through this same endpoint by accepting an `image`
field, so image-to-image needs no separate route and no separate script: when
the caller sends `image`, it is forwarded and the model edits instead of
generating.

What v5 changes, and why it is a new version rather than an edit of v2: it adds
the retry below. **Nothing else moves** -- the default mode is still `url`, so a
channel that never hits ARK's fetch timeout behaves byte-for-byte as it did on
`@v2`, and that equivalence is asserted by tests rather than claimed here.

Answering ARK's own fetch timeout
---------------------------------
ARK fetches a URL reference itself, under a hard 5 s cap nothing can raise (see
the constraints below). When that fetch is what failed, no parameter can fix it
and **the request is already over**: a non-2xx reply is raised before the
response phase runs, so a script cannot see it and cannot react to it. The one
thing that does work is not being there the second time.

The decision is made **in this file**, from the error text itself. On an upstream
failure the engine offers the script one more request phase with
`ctx.upstream_error` set; this script answers only when the failure is one it
recognises (`REFERENCE_FETCH_FAILURE` below), and the engine sends a second
upstream call only if the request that came back actually changed. A channel
therefore pays the 5 s timeout once, on the first request that meets a source ARK
cannot reach; every other request keeps the zero-download path, and a failure
this script has no answer for costs nothing but a second pass over its own
request logic.

Nothing has to be configured for that, which is the point: the channels that
needed this are the ones standing in front of us. A channel that wants none of it
at all is pinned to `@v2`, which is what the version split is for.

Two bounds, because an extra upstream call is a real cost: it happens **at most
once** -- a second failure is reported as itself -- and only while the request
still has budget. `upstream_retried` and `phase_calls["request"] == 2` on the
`adapt` span are the evidence that it happened.

The wording lives here rather than in an option because it is a fact about ARK,
and a fact that can silently stop being true: if ARK rewords the message, a
matching rule kept somewhere else would degrade to "no retry and no complaint",
which is the failure mode this mechanism exists to remove. Next to the code that
reads it, a stale string is at least reviewable in the same diff as the branch.

A channel that would rather never see that first failure can set
`image_ref_mode: "data_uri"` (or pin `@v4`, which defaults to it) and pay a
local download on every reference instead.

Known constraints (verified 2026-09-04, revised 2026-09-11):
  - Minimum pixel count 3686400: 1024x1024 is refused, so small OpenAI sizes
    are upgraded to the "2k" preset.
  - Size presets accept both cases ("2K" verified 2026-09-09 on 4-5 and
    5-0-pro), but the mapping below always emits lowercase.
  - `image` accepts a public URL and a data URI on every family measured so
    far, and bare base64 on some; which one a channel should get is the
    `image_ref_mode` decision documented at the end of this docstring.
  - The response is already OpenAI-shaped; only format mismatches need work.
  - `sequential_image_generation` is model-gated upstream: the 5-0-pro
    variants reject it with a 400 (verified 2026-09-09 on
    doubao-seedream-5-0-pro-260628), so it is only sent when the caller or
    channel options explicitly supply it.
  - The `image` wire form is model-dependent, and one earlier reading of it
    has since been falsified by measurement:
      5-0-260128  (2026-09-11, real upstream, raw body archived): URL 200;
        data URI 200 as a scalar *and* inside a two-element array; bare
        base64 400 "invalid url specified". The note here used to claim
        260128 rejected data URIs -- it does not.
      5-0-pro-260628 (2026-09-09): URL ok, data URI ok, bare base64 REJECTED.
    So URL and data URI are both accepted; bare base64 is the form neither
    family takes, and `image_ref_mode=base64` survives only for a channel
    where it was measured to work.
  - ARK fetches a URL itself, and **that fetch has a hard 5 s cap on ARK's
    side which no request parameter can raise** (方舟 FAQ, docs/6390/1359411:
    "默认图片下载超时时间5s"; 接入指南, docs/82379/2666490: "方舟服务端下载
    图片的限制：默认超时 5 秒；必须公网可访问；建议压缩至 100kB 以下"). A
    slow or overseas source therefore fails *inside* ARK -- "The parameter
    `image` specified in the request are not valid: Timeout while downloading
    url=..." -- and **it cannot be retried after the fact**: the engine makes
    exactly one upstream call per request and raises on a non-2xx reply before
    the response phase runs, so no script can react to it -- which is what the
    second request phase above is for, and why the wire form still has to be
    chosen up front for every attempt; the two answers are "give ARK no URL to
    fetch" (see below) and "give it a fast, public one".

`X-Channel-Options.image_ref_mode` picks the wire form for *every* reference,
URLs included -- a URL has no privileged exemption, because silently ignoring
the mode for one input shape is how a channel ends up gambling on ARK's 5 s:

    "url"      (default) URL -> forwarded verbatim (zero download); inline ->
               re-hosted via ctx.image_url() because ARK wants a URL
    "data_uri" any ref -> data URI; **a client URL is fetched by us** and
               inlined, so ARK needs no outbound network at all. This is the
               setting for slow or overseas sources. `inline` is accepted as
               an alias, matching google/images@v1
    "base64"   any ref -> bare base64 (no data-URI prefix); listed last
               because it is the one form measured to be rejected
"""

SMALL_SIZES = {"1024x1024", "1024x1792", "1792x1024", "512x512", "256x256"}
MID_SIZES = {"2048x2048", "2048x2560", "2560x2048"}
LARGE_SIZES = {"3840x2160", "2160x3840", "3072x3072", "4096x4096"}

#: The upstream error wording that means "ARK could not fetch a reference".
#: Matched against `ctx.upstream_error["message"]` on a retry attempt. It has to
#: appear within the first 200 characters of ARK's own message, because that is
#: all the engine carries forward from the vendor's reply.
REFERENCE_FETCH_FAILURE = "Timeout while downloading url="

# Vendor-specific knobs a caller may pass straight through.
# layer_decomposition: seedream 5-0-pro layer splitting — one input image is
# decomposed into a base image plus up to 16 transparent PNG layers; the
# response data[] then carries one entry per image with z_index/bounding_box
# metadata. prompt becomes optional in that mode (empty = auto-detect), which
# validate_images_body already allows (prompt only required without image).
# output_format: png|jpeg, accepted by the 5.0 family (official 5-0-pro
# examples pass it; the 2026-09-04 note below predates that support).
PASSTHROUGH = (
    "seed",
    "guidance_scale",
    "sequential_image_generation_options",
    "layer_decomposition",
    "output_format",
)


def _ark_size(size):
    if size in SMALL_SIZES or size in MID_SIZES:
        return "2k"
    if size in LARGE_SIZES:
        return "4k"
    return size


async def _to_ark_ref(ctx, ref):
    """Normalize any client ref into the wire form ark accepts.

    `image_ref_mode` decides the form for every shape, a URL included. The
    version before that returned URLs before the mode was read at all, so
    setting the option could not stop ARK from fetching a slow source itself --
    the one failure on this channel that nothing downstream can repair, because
    a request gets exactly one upstream call (see the module docstring). A mode
    that silently does not apply to one of the three input shapes is worse than
    no mode at all: the operator sets it, and still gets the failure.

    On a *retry* attempt the mode is overridden outright: the reason this call
    exists is that ARK could not fetch a reference, so handing it another URL
    would reproduce the failure exactly. That override is also why `base64` is
    not honoured here -- it is the form measured to be rejected, which is the
    last thing a retry should spend itself on.
    """
    ref = ref.strip()
    mode = ctx.options.get("image_ref_mode", "url")
    error = ctx.upstream_error
    if error and REFERENCE_FETCH_FAILURE in error.get("message", ""):
        # ARK itself could not fetch a reference, and no request parameter can
        # raise its 5 s cap -- so the only answer is to stop handing it a URL.
        # Every reference is inlined, not only the one named in the error: the
        # message the engine forwards is truncated and may carry a long URL.
        mode = "data_uri"
    if mode == "inline":
        # The name google/images@v1 uses for this same behaviour. Accepting it
        # is not politeness: an operator who set `inline` on a google channel
        # will try the same value here, and an unrecognised value falls through
        # to "url" -- i.e. straight back into the 5 s gamble this option exists
        # to avoid. A silent no-op is the failure mode being fixed.
        mode = "data_uri"
    if mode == "base64":
        # Handles both data URIs and bare base64, and validates the encoding.
        # A URL is fetched first, like any other shape.
        return await ctx.image_b64(ref)
    if mode == "data_uri":
        # Downloads a URL and sniffs the mime, which is what lets ARK serve
        # the request without fetching anything.
        return await ctx.image_data_uri(ref)
    if ctx.is_url(ref):
        # "url" mode: zero download here; ARK fetches it under its own 5 s cap.
        return ref
    return await ctx.image_url(ref)


async def transform(ctx, payload, phase):
    if phase == "request":
        body = {
            "model": ctx.options.get("model", "doubao-seedream-5-0-260128"),
            "prompt": payload.get("prompt", ""),
            "size": _ark_size(payload.get("size", "1024x1024")),
            "response_format": payload.get("response_format", "url"),
            # Payload wins over channel options, mirroring every other
            # per-request knob.
            "watermark": payload.get("watermark", ctx.options.get("watermark", True)),
            "stream": False,
        }

        # Model-gated upstream (5-0-pro variants 400 on it), so the field is
        # only forwarded when explicitly requested instead of defaulted on.
        seq = payload.get(
            "sequential_image_generation",
            ctx.options.get("sequential_image_generation"),
        )
        if seq is not None:
            body["sequential_image_generation"] = seq

        image = payload.get("image")
        if image:
            if isinstance(image, list):
                refs = []
                for item in image:
                    refs.append(await _to_ark_ref(ctx, item))
                body["image"] = refs if len(refs) > 1 else refs[0]
            else:
                body["image"] = await _to_ark_ref(ctx, image)

        for key in PASSTHROUGH:
            value = payload.get(key)
            if value is not None:
                body[key] = value

        return body

    # response phase: ark already returns {"data": [...], "created": ...}.
    # usage is billing-relevant (layer decomposition bills per returned
    # image), so it is forwarded whenever ark provides it.
    #
    # No output-shape normalisation here, deliberately. `response_format` is
    # forwarded above and this upstream honours both values, so `data` already
    # has the shape the caller asked for. Only upstreams that lie about
    # response_format need the url <-> b64_json conversion that
    # openai/images@v1 and google/images@v1 do -- see docs/05 §2.
    out = {
        "created": payload.get("created", 0),
        "data": payload.get("data", []),
    }
    if payload.get("usage") is not None:
        out["usage"] = payload["usage"]
    return out
