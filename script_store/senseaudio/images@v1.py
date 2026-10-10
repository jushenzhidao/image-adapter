"""senseaudio/images@v1: SenseAudio open-platform image generation, sync direct.

Channel setup (New API side):
  X-Upstream-Url:  https://api.senseaudio.cn/v1/image/sync
  X-Script-Ref:    senseaudio/images@v1
  Authorization:   Bearer <SENSEAUDIO_API_KEY>

  Standard bearer auth -- the engine's default emit is already correct, so
  unlike fal no X-Auth-Emit is needed and the script never touches the
  credential. Nothing else is required: the upstream is synchronous (the
  connection blocks until the image exists, 15-27s measured), which is the
  shape this adapter's one-request-one-result contract wants -- no X-Async,
  no polling, no task ids. The platform also exposes an async twin
  (/v1/image/async + /v1/image/pending); it is deliberately unused.

One endpoint, two modes
-----------------------
Like ark (and unlike fal), generation and edit share one endpoint: a request
carrying `image` is a reference-guided generation, everything else is
text-to-image. The wire field is `reference` -- a *single string*, URL or
data URI -- so the canonical list collapses to its first entry, and more
than one reference is refused here with the count named: the upstream has no
list to truncate into, and silently dropping references is exactly the
failure this adapter refuses to dress up as success.

Models
------
The wire `model` is required. Precedence follows the house rule (ark, fal):

    X-Model-Map match  >  the three platform models  >  seedream fallback

Every name the platform does not know folds to `doubao-seedream-5-0-260128`
-- the only seedream it sells, the cheapest per image, and the one table
verified end to end. A channel that routes `doubao-seedream-4-0` or any
other legacy or mistaken name keeps producing pictures instead of paying
for a `502 upstream_model_not_found`; a *mapped* value, being an explicit
operator decision, is used verbatim and never second-guessed. Even a
missing model falls back rather than refusing: this channel's promise is a
picture, and an absent name is a routing question, not a caller apology.

Sizes
-----
Every model owns a fixed size table (WxH enum); an off-table size is a flat
upstream 400 whose body the engine does not carry -- a caller would see a
bare `upstream_http_error` with no field named. The script therefore repairs
what it can, in the bfl tradition, with one intent-preserving rule:

  1. an exact table hit rides through untouched;
  2. else, entries with the caller's exact aspect ratio (integer
     cross-multiplication, no float slop) compete by nearest pixel area;
  3. else the smallest geometric distance in log space wins, searched
     only within the caller's orientation -- a portrait request never
     lands on a landscape neighbour.

The size slot also accepts tier words ("1K" / "2K" / "4K", case-insensitive):
the entry whose long edge is nearest K*1024, shape-blind -- "4K" on
seedream is 4096x2304, the closest thing to what the word promises.

Missing or unparseable sizes fall back to the table's smallest entry. The
docs read "size is required when no reference is given" as *optional with
one* -- the live upstream disagrees: a reference without a size is a 400
"参数错误：size" (2026-10-10, all four doors). Since canonical `size` is an
optional field (OpenAI's "auto"), a required-but-absent size is exactly
what a script repairs, and the cheapest table entry is the closest thing
this platform has to auto. This is also what keeps the chat/responses
doors alive: their whitelist folding carries no size, so anything stricter
would make three of the four front doors dead on arrival for this channel.
A mapped model with no table has no fallback to offer -- its size rides
through for the upstream to judge.

Inputs
------
The body is rebuilt as a whitelist:

    prompt      required, always -- senseaudio documents prompt as required
                in both modes, and its 400s carry no field detail, so an
                empty one is refused here by name
    reference   see "One endpoint, two modes"
    seed        forwarded when the caller supplies a real int; booleans are
                ints in Python and read as a typo here, dropped
    size        see "Sizes"

    response_format / n / style / quality and every other canonical knob:
    not upstream vocabulary -- not forwarded.

What the reply owes
-------------------
senseaudio answers `{"url": "..."}` -- no envelope, no b64 option, no seed.
The script rebuilds the canonical envelope around it:

  * an http(s) link rides through as-is, or through `rehost_url: true` (the
    cross-channel convention) which swaps it for one of ours; a rehost with
    no object storage keeps the vendor link. The links are unsigned OSS+CDN
    objects with no observed expiry -- sturdier than ark's 24h window --
    but the platform's retention policy is unpublished, so `rehost_url`
    remains the lever a durability-minded caller reaches for.
  * a caller who asked for b64_json gets the link fetched and encoded here
    (`ctx.image_b64`), the promised bytes whatever the wire carried.
  * a data URI in the url slot (never observed, defended anyway) is decoded
    and stored -- or degrades to b64_json when no storage exists; a data
    URI is never parked in `url`, which is not a URL.
  * a 200 without a usable url fails loudly (`upstream_invalid_response`):
    a silent empty `data` looks like success to every caller that only
    checks the status.

`response_format` is parked per request id and consumed exactly once by the
reply, the same pocket fal and tencent_ci use.

Known bounds
------------
  * `sensenova-u1-fast` returned 502 `upstream_model_not_found`
    (ref_code 502101) on every probe (2026-10-10, two sizes, sub-second
    replies): the platform lists it but its upstream is down. Its size
    table is kept here so the day it heals the channel routes it correctly
    with no script change.
  * Prompt budgets are upstream-enforced (6000 code points on image-2.0,
    2000 elsewhere); the script counts nothing.
  * Reference-guided generation is unmeasured (no size derived from a
    reference was billed during verification); the pass-through follows the
    documented contract.

Provenance: `@v1` is the first and only version. Facts in
`capabilities/senseaudio.json` come from docs.senseaudio.cn (checked
2026-10-10) plus a live probe: image-2.0 and seedream verified by traffic
(seven billable generations across both, outputs byte-checked against the
requested tables), u1-fast verified unavailable. Unit tests pin the script
against fabrications, never against the billable path.
"""

import math
import time

#: `doubao-seedream-*` folds here -- the platform's only seedream model.
SEEDREAM_CANONICAL = "doubao-seedream-5-0-260128"

#: Size tables per model, document order (docs.senseaudio.cn, 2026-10-10).
#: image-2.0 splits into a <=1.5K pricing tier (w*h <= 3_000_000) and a 2K
#: tier; the split matters for billing, not for this script -- the tables
#: are flat lookups either way.
SIZE_TABLES = {
    "senseaudio-image-2.0-260319": (
        (1024, 1024), (1024, 1280), (1280, 1024),
        (1536, 864), (864, 1536), (1024, 1536), (1536, 1024),
        (2016, 864), (864, 2016), (2048, 1024), (1024, 2048),
        (2048, 1152), (1152, 2048), (2048, 1360), (1360, 2048),
        (2688, 1152), (1536, 2048), (2048, 1536),
        (2688, 1344), (1344, 2688), (3136, 1344),
    ),
    SEEDREAM_CANONICAL: (
        (2304, 1728), (1728, 2304), (2496, 1664), (1664, 2496),
        (2048, 2048), (3136, 1344), (2848, 1600), (1600, 2848),
        (3456, 2592), (2592, 3456), (2496, 3744), (3744, 2496),
        (4096, 2304), (2304, 4096), (3072, 3072), (4704, 2016),
    ),
    "sensenova-u1-fast": (
        (1664, 2496), (2496, 1664), (1760, 2368), (2368, 1760),
        (1824, 2272), (2272, 1824), (2048, 2048),
        (2752, 1536), (1536, 2752), (3072, 1376), (1344, 3136),
    ),
}

#: Tier words the size slot also accepts ("4K", "2k", case-insensitive):
#: target long edge = K*1024, matched to the table entry whose long edge is
#: nearest (ties by nearest area to the square of the target). The wire has
#: no tier vocabulary of its own -- this is a caller-convenience translation,
#: and the WxH table stays the only source of truth.
TIER_TARGETS = {"1k": 1024, "2k": 2048, "4k": 4096}

#: `response_format` parked per request id, consumed once by the reply.
_STATE = {}


def resolve_model(payload, mapped_model):
    """The wire model: X-Model-Map > known platform models > seedream.

    The fallback is deliberate: an unknown name spent on the upstream buys a
    502 whose body the engine does not carry; spent on the seedream it buys
    a picture. `SEEDREAM_CANONICAL` is itself a table key, so the known
    models (and the bare/prefixed seedream spellings before the fold) all
    resolve here without a second allowlist to drift.
    """
    if mapped_model:
        return str(mapped_model)
    model = str(payload.get("model") or "")
    if model == "doubao-seedream" or model.startswith("doubao-seedream-"):
        return SEEDREAM_CANONICAL
    return model if model in SIZE_TABLES else SEEDREAM_CANONICAL


def _parse_size(size):
    """A canonical "WxH" as a pixel pair, or None when it does not parse."""
    if not isinstance(size, str):
        return None
    width, sep, height = size.partition("x")
    if not sep:
        return None
    try:
        w, h = int(width), int(height)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return w, h


def _nearest(table, width, height):
    """The caller's intent-preserving neighbour on a model's size table.

    Exact table hit first; then same-shape entries (integer cross
    multiplication -- `w1*h2 == w2*h1` has no float slop) by nearest pixel
    count; then log-space distance *within the caller's orientation*: a
    portrait request never lands on a landscape neighbour, because log
    compresses a 6x width gap enough for a small height gap to outvote it
    -- 300x1700 must not come back as 2304x1728. Ties fall to table order
    via min().
    """
    if (width, height) in table:
        return width, height
    same_shape = [
        (w, h) for (w, h) in table if width * h == w * height and (w, h) != (width, height)
    ]
    if same_shape:
        return min(same_shape, key=lambda wh: abs(wh[0] * wh[1] - width * height))
    landscape = width > height
    oriented = [
        (w, h) for (w, h) in table if (w > h) == landscape and (w, h) != (width, height)
    ]
    return min(
        oriented or table,
        key=lambda wh: abs(math.log(width / wh[0])) + abs(math.log(height / wh[1])),
    )


def _resolve_size(model, size):
    """The wire size: tier word, exact hit, oriented neighbour, or auto.

    Tier words ("4K" et al.) pick the entry whose long edge is nearest
    K*1024 -- shape-blind by nature, so they bypass the aspect-ratio rules
    entirely. A missing/unparseable WxH falls back to the cheapest table
    entry -- the platform has no "auto", and this is what stands in for it
    (see "Sizes"). None means a mapped model with no table: nothing to
    repair with, the size question goes to the upstream unserved.
    """
    table = SIZE_TABLES.get(model)
    if table is None:
        pixels = _parse_size(size)
        return f"{pixels[0]}x{pixels[1]}" if pixels else None
    if isinstance(size, str):
        target = TIER_TARGETS.get(size.strip().lower())
        if target:
            w, h = min(table, key=lambda wh: (abs(max(wh) - target),
                                              abs(wh[0] * wh[1] - target * target)))
            return f"{w}x{h}"
    pixels = _parse_size(size)
    if pixels is None:
        w, h = min(table, key=lambda wh: wh[0] * wh[1])
        return f"{w}x{h}"
    w, h = _nearest(table, *pixels)
    return f"{w}x{h}"


async def _reply_item(ctx, requested, url):
    """The one upstream url as a canonical data[] item -- see the docstring."""
    if not isinstance(url, str) or not url:
        ctx.fail("upstream returned no usable image url", code="upstream_invalid_response")
    if url.startswith("data:"):
        stored = await ctx.upload_temp_image(ctx.decode_b64(url), ext="jpeg")
        if ctx.is_url(stored):
            return {"url": stored}
        return {"b64_json": ctx.encode_b64(ctx.decode_b64(url))}
    if requested == "b64_json":
        return {"b64_json": await ctx.image_b64(url)}
    if ctx.options.get("rehost_url") is True:
        stored = await ctx.rehost_image(url)
        return {"url": stored if stored is not None else url}
    return {"url": url}


async def _response(ctx, payload):
    """senseaudio's bare `{"url": ...}` as the canonical envelope."""
    requested = _STATE.pop(ctx.request_id, None)
    item = await _reply_item(ctx, requested, payload.get("url"))
    return {"created": int(time.time()), "data": [item]}


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = payload.get("response_format")
    if requested == "url" or requested == "b64_json":
        _STATE[ctx.request_id] = requested

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        # Required upstream in both modes, and senseaudio's 400s carry no
        # field detail: refusing here is the only place the field gets named.
        ctx.fail("senseaudio requires a prompt", param="prompt")

    image = payload.get("image")
    refs = image if isinstance(image, list) else ([image] if image else [])
    if len(refs) > 1:
        # The wire field is a single string; a list has no upstream meaning,
        # and dropping the extras would pass off a different picture as the
        # answer to the one the caller wrote.
        ctx.fail(
            "senseaudio accepts a single reference image, got "
            f"{len(refs)}",
            param="image",
        )

    model = resolve_model(payload, ctx.mapped_model)

    size = _resolve_size(model, payload.get("size"))
    if size is None:
        # Only a mapped model with no table lands here: nothing to repair
        # with, and the upstream's verdict is the only one left.
        ctx.fail("no size table behind this model; pass an explicit WxH",
                 param="size")

    body = {"model": model, "prompt": prompt}
    if size is not None:
        body["size"] = size
    if refs:
        body["reference"] = refs[0]
    seed = payload.get("seed")
    # `isinstance(True, int)` is True in Python; a boolean seed is a typo,
    # dropped rather than forwarded as 1.
    if isinstance(seed, int) and not isinstance(seed, bool):
        body["seed"] = seed
    return body
