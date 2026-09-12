"""volcengine_ark/images@v8: doubao-seedream text-to-image and image-to-image.

Channel setup (New API side):
  X-Upstream-Url: https://ark.cn-beijing.volces.com/api/v3/images/generations
  X-Script-Ref:   volcengine_ark/images@v8
  Authorization:  Bearer <ARK_API_KEY>
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128"}  (optional)

  Nothing else is required. This version keeps the fast path -- a URL reference
  is forwarded verbatim, so ARK fetches it and we download nothing -- and falls
  back to fetching that reference ourselves when ARK reports it could not. See
  "Answering ARK's own fetch timeout" below.
  Optional, to skip the fast path entirely and always inline:
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128",
                      "image_ref_mode": "data_uri"}   ("inline" also accepted)
  Optional, to shrink the references we hand over:
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128",
                      "ref_max_edge": 2048, "ref_max_bytes": 102400,
                      "ref_fmt": "webp", "ref_quality": 85}

Seedream exposes editing through this same endpoint by accepting an `image`
field, so image-to-image needs no separate route and no separate script: when
the caller sends `image`, it is forwarded and the model edits instead of
generating.

What v8 changes, and why it is a new version rather than an edit of `@v7`: two
things, and a channel can observe both.

  * **The `ref_*` option group from `@v3`.** `@v5` was written against `@v2`, so
    the compression policy `@v3` added and the default flip `@v4` added were
    both left behind by the `@v5` -> `@v6` -> `@v7` line, which carries neither.
    This version merges the two lines: `@v8` = `@v7` (watermark off, both retry
    wordings, default `image_ref_mode` = `url`) + the `ref_*` policy. See
    "Reference compression" below.
  * **References in an `image` array materialise concurrently.** See
    "Concurrent reference materialisation" below. The *body* a channel that set
    no `ref_*` sends is byte-for-byte `@v7`'s -- asserted by tests rather than
    claimed here -- while the number of requests we keep in flight on the
    caller's origin is not.

`@v4`'s default flip is deliberately **not** carried over: `url` stays the
default here, with the retry below as the answer to ARK's 5 s cap. A channel
that wants the always-inline behaviour sets `image_ref_mode: "data_uri"` (or
pins `@v4`, which defaults to it).

Concurrent reference materialisation
------------------------------------
The references in an `image` array go through ``ctx.fanout``, so N of them cost
one wait instead of N. This is the fan-out `openai/images@v2` and
`google/images@v2` already use, and it is the A4 item the phase-optimisation
plan left open (reports/2026-09-12_phase-optimization/PLAN.md §2.7).

`@v3` did this serially on purpose, and that reason has to be answered rather
than dropped: compression is CPU, ``binascii`` and Pillow hold the GIL, and
overlapping CPU work adds concurrency without adding throughput. The argument
holds for the *re-encode* and only for the re-encode. What a reference actually
costs is dominated by the waits around it -- fetching a URL, uploading a
re-hosted one -- and those are what the fan-out hides; the re-encode inside each
item still runs one at a time, because the GIL is still there. In the default
`url` mode with no `ref_*` set, N references are N uploads and no downloads; in
`data_uri` mode they are N downloads. Both are waits.

The degree is bounded by ``fanout_concurrency`` (docs/07 §14.2.1): peak
concurrency is ``min(fanout_concurrency, N)``, a single reference still takes
the serial arm, and the failure semantics are the serial loop's -- the earliest
failing item in input order reaches the caller, with its own status, rather
than an `ExceptionGroup`.

What that costs, stated plainly: a channel whose caller sends many references
now puts up to `fanout_concurrency` requests on that caller's origin at once,
where the serial loop put one. The body is unchanged; the timing is not.

Reference compression (`ref_*` options)
---------------------------------------
A reference is the biggest thing a client sends and the one thing that can fail
on ARK's side rather than ours: ARK fetches our URL itself under a hard 5 s cap
that no request parameter can raise, and the same page that documents the cap
recommends keeping the image **below 100 kB**. Shrinking the reference is
therefore the answer to both halves of the problem -- the fetch that times out
and the body that is larger than it needs to be.

    ref_max_edge    longest-edge ceiling in pixels. Hard: the image is scaled
                    down when it exceeds it, never up
    ref_max_bytes   byte target. Best effort -- see below
    ref_fmt         jpeg | webp | png | gif. Absent means "keep the source
                    format", so a channel that has not checked which encodings
                    the model accepts is never surprised by one. Setting it is
                    a format instruction and is honoured even when that format
                    comes out larger -- pair it with `ref_max_bytes` when size
                    is the point. Only a re-encode that keeps the format *and*
                    fails to shrink is discarded, in favour of the bytes we
                    were given
    ref_quality     1..100, for JPEG and WEBP. **Requires `ref_fmt`**, because
                    an option that silently does nothing is worse than an
                    option that is absent

Three rules the implementation follows, each of them the answer to a way this
could go wrong quietly:

  * **A URL in `url` mode is never touched.** That mode's whole promise is zero
    download on our side; fetching a reference in order to compress it would
    trade a documented 5 s risk for an unbounded one. Compressing a reference
    requires us to hold its bytes, so it happens exactly where we already hold
    them -- which is everywhere else: an inline reference in `url` mode (we
    upload it), and every reference in `data_uri`/`base64` mode (we inline it).
    There is no input shape on which the options silently do not apply.
  * **A reference we cannot decode is passed through, not refused.** The
    allowlist in `ctx.image` covers PNG/JPEG/WEBP/GIF while the front door
    accepts more than that, so a BMP or AVIF reference is a normal request that
    this policy simply cannot help with. Failing it would make an optimisation
    turn a working request into a 400, so the original bytes are used and the
    request proceeds exactly as it did on `@v7`. Decode refusals only, though:
    a reference we failed to *fetch* still fails the request, because that is a
    fact about the URL rather than about the optimisation, and retrying it here
    would spend the same timeout twice.
  * **A malformed option is a channel error, not a client error.** `ref_fmt:
    "tiff"` or `ref_quality: 0` fails with `channel_config_error` before any
    upstream call, naming the option. Reporting it against the client's image
    would send the operator to the wrong layer -- the same reasoning that keeps
    `ctx.download_image` from answering an inline reference with a channel
    error.

`ref_max_bytes` is a target and not a guarantee: it can only be met by spending
quality or pixels, so the search is bounded (quality first, down to a floor,
then dimensions -- and only when the smaller image actually fits). A reference
that cannot reach the target comes back at full size rather than mutilated for
nothing; `ref_max_edge` is the lever for a hard ceiling. That the policy ran at
all is visible on the `storage_put` span, whose `ext` and `bytes` describe what
was actually uploaded.

The retry overrides the policy's wire form, not the policy itself: on a retry
attempt every reference is inlined (see below), and a reference that is inlined
is one whose bytes we hold, so `ref_*` applies to it.

Watermark is off unless someone asks for it
-------------------------------------------
`watermark` is sent as `false` when neither the caller nor the channel options
name it, so a channel that never mentions watermark produces clean images.

It cannot be expressed by *omitting* the field, which is why the default is
spelled out instead of left to the upstream: ARK's own default for this
parameter is `true` (docstring of the 5.0 family; confirmed by a real image
carrying the mark in the bottom-right corner, archived at
reports/2026-09-11_online-env/REPORT.md). Omitting it therefore means "on",
and the earlier reading of a clean default was simply an upstream reading.

The precedence chain is -- request body, then channel option, then the default
-- so only the last link moved:

    payload["watermark"]  >  X-Channel-Options.watermark  >  False

A caller that does want the mark still gets it, per request or per channel.
That is deliberate rather than incidental: this reverses the default, it does
not remove the capability, and a channel that genuinely needs the mark is not
asked to rewrite a script to keep it.

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
recognises (`REFERENCE_FAILURES` below), and the engine sends a second
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
  - `watermark` defaults to `true` upstream, so "off" has to be sent as a
    value rather than left unsaid (see above). A `null` is not a way to say
    "unset" either: it is forwarded as `null`, because the field is taken from
    the body verbatim when present.
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
    chosen up front for every attempt; the three answers are "give ARK no URL
    to fetch" (`data_uri`), "give it a fast, public one", and "give it a
    smaller one" (`ref_*`).
  - The 100 kB recommendation above is also why `ref_max_bytes` accepts a
    value that the reference may not reach: a PNG screenshot can usually be
    brought under it, an already-compressed JPEG often cannot, and pretending
    otherwise would mean returning a picture nobody asked for.

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

from functools import partial

SMALL_SIZES = {"1024x1024", "1024x1792", "1792x1024", "512x512", "256x256"}
MID_SIZES = {"2048x2048", "2048x2560", "2560x2048"}
LARGE_SIZES = {"3840x2160", "2160x3840", "3072x3072", "4096x4096"}

#: The ARK wordings that mean "your reference did not get through".
#: Matched against `ctx.upstream_error["message"]` on a retry attempt. Each has
#: to appear within the first 200 characters of ARK's own message, because that
#: is all the engine carries forward from the vendor's reply.
REFERENCE_FAILURES = (
    # ARK went and fetched the reference itself, and gave up: the 5 s cap it
    # imposes on that download. Inlining answers it by leaving it nothing to
    # fetch.
    "Timeout while downloading url=",
    # ARK would not accept the reference *as a URL* -- which is what a bare
    # base64 value produces, with no `data:image/...;base64,` prefix to make it
    # one. Inlining puts it into the form ARK does accept.
    #
    # The same wording also covers a URL so malformed that ARK rejects it. There
    # inlining means we try to fetch it ourselves, which either works (and the
    # retry fixes the request) or fails with our own error -- a different
    # message, but the same refusal, and it costs nothing but the attempt.
    "invalid url specified",
)

#: Target formats `ref_fmt` may name. Mirrors ``imageops.FORMAT_ALIASES`` in the
#: framework, which a script cannot import -- so this list has to be repeated
#: here, and is checked first so a typo is reported against the option that
#: carries it rather than against the client's image.
REF_FORMATS = ("jpeg", "jpg", "webp", "png", "gif")

#: Formats where `ref_quality` means something.
REF_LOSSY_FORMATS = ("jpeg", "jpg", "webp")

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


def _fail_config(ctx, message):
    """An unusable `ref_*` option, reported as the control plane's mistake.

    The code matters more than the text: `channel_config_error` says "the
    channel headers are wrong", which is where the operator looks. A 400
    carrying `image_invalid` would send them to the client's file instead.
    """
    ctx.fail(message, code="channel_config_error")


def _int_option(ctx, name, value):
    """One numeric `ref_*` option read as an integer.

    Accepts an int or a string of digits -- operators write both, and a header
    typed by hand is as likely to carry `"2048"` as `2048` -- and refuses
    anything else, a float included: `2048.5` is a typo, and truncating it
    silently is exactly the kind of guess an option parser should not make.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail_config(ctx, f"X-Channel-Options.{name} must be an integer")
    try:
        return int(str(value).strip())
    except ValueError:
        _fail_config(ctx, f"X-Channel-Options.{name} must be an integer")


def _positive_int(ctx, name, value):
    """A numeric option that is also a count: edges and byte targets."""
    number = _int_option(ctx, name, value)
    if number <= 0:
        _fail_config(ctx, f"X-Channel-Options.{name} must be positive, got {number}")
    return number


def _ref_format(ctx, value):
    if not isinstance(value, str) or value.strip().lower() not in REF_FORMATS:
        _fail_config(
            ctx,
            "X-Channel-Options.ref_fmt must be one of "
            f"{list(REF_FORMATS)}, got {value!r}",
        )
    return value.strip().lower()


def _ref_quality(ctx, value, fmt):
    """Reads `ref_quality`, which is meaningless without a lossy `ref_fmt`.

    Refusing the combination instead of ignoring it is the whole point: an
    operator who sets a quality on a PNG target has asked for something that
    cannot happen, and a silent no-op would leave them believing it did.
    """
    number = _int_option(ctx, "ref_quality", value)
    if not 1 <= number <= 100:
        _fail_config(ctx, f"X-Channel-Options.ref_quality must be 1..100, got {number}")
    if fmt not in REF_LOSSY_FORMATS:
        _fail_config(
            ctx,
            "X-Channel-Options.ref_quality requires ref_fmt to be one of "
            f"{list(REF_LOSSY_FORMATS)}; a PNG or GIF target has no quality knob",
        )
    return number


def _ref_policy(ctx):
    """The reference-compression policy, or None when the channel set none.

    None is the important return value: it is what keeps a channel that never
    opted in on the exact call sequence `@v7` used, so the promotion cannot
    change its body.
    """
    edge = ctx.options.get("ref_max_edge")
    target = ctx.options.get("ref_max_bytes")
    fmt = ctx.options.get("ref_fmt")
    quality = ctx.options.get("ref_quality")
    if edge is None and target is None and fmt is None and quality is None:
        return None

    policy = {}
    if edge is not None:
        policy["max_edge"] = _positive_int(ctx, "ref_max_edge", edge)
    if target is not None:
        policy["max_bytes"] = _positive_int(ctx, "ref_max_bytes", target)
    if fmt is not None:
        policy["fmt"] = _ref_format(ctx, fmt)
    if quality is not None:
        policy["quality"] = _ref_quality(ctx, quality, policy.get("fmt"))
    return policy


#: Adapter error codes that mean "this image cannot be shrunk", where the right
#: answer is to send it as it arrived. Everything else -- a download that
#: failed, a bug in the compressor -- is re-raised, because swallowing those
#: would turn a real failure into a request that merely looks unoptimised.
KEEP_ORIGINAL_CODES = ("image_invalid", "image_format_unsupported", "image_too_large")


def _code_of(exc):
    """The adapter error code, or None for anything that is not one.

    Written without ``getattr`` because the sandbox forbids that name; the
    attribute error is the test for "this is not an adapter error".
    """
    try:
        return exc.code
    except AttributeError:
        return None


async def _compressed_or_kept(ctx, ref, policy):
    """The reference's bytes, shrunk when the policy allows it.

    An image this build cannot decode is not a failure of the request: the
    allowlist covers PNG/JPEG/WEBP/GIF while the front door accepts more, so a
    BMP reference is a normal request that the policy simply cannot help with.
    It proceeds with its original bytes, exactly as it did on `@v7` -- an
    optimisation must not turn a working request into a 400.
    """
    try:
        return await ctx.compress_image(ref, **policy)
    except Exception as exc:
        if _code_of(exc) not in KEEP_ORIGINAL_CODES:
            raise
        return await ctx.image_bytes(ref)


async def _to_ark_ref(ctx, ref):
    """Normalize any client ref into the wire form ark accepts.

    `image_ref_mode` decides the form for every shape, a URL included. The
    version before that returned URLs before the mode was read at all, so
    setting the option could not stop ARK from fetching a slow source itself --
    the one failure on this channel that nothing downstream can repair, because
    a request gets exactly one upstream call (see the module docstring). A mode
    that silently does not apply to one of the three input shapes is worse than
    no mode at all: the operator sets it, and still gets the failure.

    On a *retry* attempt the mode is overridden outright, and it is overridden
    before the `url` fast path is consulted: the reason this call exists is that
    ARK could not fetch a reference, so handing it another URL would reproduce
    the failure exactly. That override is also why `base64` is not honoured
    there -- it is the form measured to be rejected, which is the last thing a
    retry should spend itself on.

    The `ref_*` policy follows the same rule and is read before the mode is
    applied, so it governs every reference whose bytes pass through us -- which,
    after the override above, is every reference on a retry.
    """
    ref = ref.strip()
    mode = ctx.options.get("image_ref_mode", "url")
    error = ctx.upstream_error
    message = error.get("message", "") if error else ""
    if any(reason in message for reason in REFERENCE_FAILURES):
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

    if mode == "url" and ctx.is_url(ref):
        # "url" mode: zero download here; ARK fetches it under its own 5 s cap.
        # A reference we never read the bytes of is a reference `ref_*` cannot
        # apply to, and that is stated rather than discovered: fetching it in
        # order to shrink it would spend the same 5 s gamble this mode avoids.
        return ref

    policy = _ref_policy(ctx)
    if policy is None:
        # No policy: the exact `@v7` call sequence, kept verbatim so "the
        # channel did not opt in" is provably the same request as before.
        if mode == "base64":
            # Handles both data URIs and bare base64, and validates the encoding.
            # A URL is fetched first, like any other shape.
            return await ctx.image_b64(ref)
        if mode == "data_uri":
            # Downloads a URL and sniffs the mime, which is what lets ARK serve
            # the request without fetching anything.
            return await ctx.image_data_uri(ref)
        return await ctx.image_url(ref)

    # A policy: materialise the bytes once, shrink them, then shape them. The
    # three conversions below are the same three the branch above performs,
    # minus the redundant decode each of them would repeat.
    data = await _compressed_or_kept(ctx, ref, policy)
    if mode == "base64":
        return ctx.encode_b64(data)
    if mode == "data_uri":
        return ctx.data_uri(data, mime=ctx.sniff_mime(data))
    mime = ctx.sniff_mime(data)
    subtype = mime.split("/", 1)[1] if mime.startswith("image/") else "png"
    return await ctx.upload_temp_image(data, ext=subtype)


async def transform(ctx, payload, phase):
    if phase == "request":
        body = {
            "model": ctx.options.get("model", "doubao-seedream-5-0-260128"),
            "prompt": payload.get("prompt", ""),
            "size": _ark_size(payload.get("size", "1024x1024")),
            "response_format": payload.get("response_format", "url"),
            # Off unless asked for, and stated rather than omitted because ARK's
            # own default is on -- see "Watermark is off unless someone asks for
            # it" in the module docstring. Payload wins over channel options,
            # mirroring every other per-request knob.
            "watermark": payload.get("watermark", ctx.options.get("watermark", False)),
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
                # Concurrently, because what N references cost is N waits --
                # fetches, re-host uploads -- and the fan-out turns those into
                # one wait. See "Concurrent reference materialisation" in the
                # module docstring for the two things this changes and the one
                # it deliberately does not.
                #
                # `refs` keeps the input order whatever order the items finish
                # in, a single reference still takes the serial arm, and a
                # failure reaches the caller as the earliest failing item's own
                # exception with its own status -- ctx.fanout re-raises that
                # rather than an ExceptionGroup, which the engine would collapse
                # into a 500.
                refs = await ctx.fanout(image, partial(_to_ark_ref, ctx))
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
