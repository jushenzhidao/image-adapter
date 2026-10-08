"""fal/images@v1: FLUX.3 text-to-image and edit-image, synchronous direct run.

Channel setup (New API side):
  X-Upstream-Url:  https://fal.run/blackforestlabs/flux-3/text-to-image
  X-Script-Ref:    fal/images@v1
  X-Auth-Emit:     header:Authorization:Key
  Authorization:   Bearer <FAL_KEY>

  fal authenticates with the literal word Key, not Bearer, and the engine
  already knows how to say that: the channel's Authorization is stripped of
  its Bearer prefix into the bare upstream key, and `X-Auth-Emit:
  header:Authorization:Key` spells it back out with fal's own prefix. The
  script never touches the credential.

  Nothing else is required. The upstream is synchronous: `fal.run` blocks
  the connection until the image exists, which is exactly the shape this
  adapter's one-request-one-result contract wants -- no X-Async, no polling,
  no task ids.

Two endpoints, one script
-------------------------
fal splits the FLUX.3 app by operation, the way the whole platform does:
`blackforestlabs/flux-3/text-to-image` and `blackforestlabs/flux-3/edit-image`
are two endpoint ids, not one endpoint with an image field (contrast ark,
where a single `images/generations` URL serves both). The script therefore
steers the outbound call with `ctx.emit(url=...)` -- the same power the
tencent_ci script uses to build a signed COS URL -- picking the path by one
rule: a request carrying `image` goes to the edit twin, everything else stays
on the channel's configured entry.

The twin is resolved from whatever path the channel carries, so a deployment
that fronts fal with its own mirror keeps working when only the host changes:

    .../text-to-image   ->  .../edit-image      (suffix swapped)
    .../flux-3          ->  .../flux-3/edit-image (suffix appended)

`X-Model-Map` reaches other endpoints on the platform the same way: a mapped
value is the full fal endpoint id (`fal-ai/flux-pro/v1.1-ultra`, say), used
verbatim on the generate side. Its edit side follows fal's naming convention
-- swap a `text-to-image` suffix for `edit-image` -- and a mapped id that
carries neither suffix has no twin this script can name, so an edit request
against one fails loudly instead of inventing a path: a fabricated suffix is
a call to an endpoint nobody verified.

Inputs
------
The body is rebuilt as a whitelist; nothing the caller sends rides along
uninvited. What the script consumes:

    prompt          required; the edit twin requires it too, and a request
                    with images but no prompt is refused HERE with the field
                    named, not spent on a 422 from fal
    size "WxH"      fal has no size field. It takes an aspect-ratio enum and
                    a resolution tier, so a size is *derived* into both: the
                    ratio is the nearest enum neighbour of w/h, the tier is
                    picked by the long edge (<=512 -> 512sq, <=768 -> 768sq,
                    <=1536 -> 1k, <=3072 -> 2k, else 4k). A size that does
                    not parse is dropped, not refused -- fal's own defaults
                    (auto / 1k) are a working answer, and a 400 over a
                    decorative size trades an image for a technicality.
    aspect_ratio    an explicit value wins over the derivation; a
    resolution      non-string is dropped as the obvious typo it is. The
                    enums are fal's own spellings and a listed-but-wrong
                    spelling is fal's 422 to give, not ours to preempt.
    response_format "b64_json" forces `sync_mode: true` -- fal has no b64
                    output switch, and the data URI that sync_mode returns
                    is the only wire form carrying bytes, so that is the one
                    lever that honours the caller's ask. "url" (or nothing)
                    leaves sync_mode unset and keeps fal's CDN link.
    sync_mode       an explicit caller value is honoured when it does not
                    collide with the promise above; `b64_json` outranks an
                    explicit false, because a canonical answer was promised
                    and "I asked for bytes and got a link" is the failure
                    this field exists to prevent.
    n               not forwarded: one request, one image, upstream's own
                    semantics (the gate already repairs malformed n to 1).
    image           the edit input. URL and data URI both ride through
                    verbatim -- fal fetches URLs server-side and decodes
                    data URIs natively, so unlike ark there is no measured
                    fetch-timeout gamble to answer and no re-encoding to
                    do. More than 10 references fails here with the count;
                    fal documents the cap, and the caller should see it
                    named before the request is spent. An empty list never
                    reaches this script: the gate drops it as "no image"
                    (canonical semantics -- null / [] mean no reference),
                    so `image: []` IS a text-to-image request by contract,
                    and `edit` below simply follows the payload's truth.

    safety_tolerance, enable_prompt_expansion, output_format, version:
                    forwarded when the caller supplies them (fal's own
                    optional knobs, documented spellings).

What the reply owes
-------------------
fal answers `{"images": [{url, content_type, file_name, file_size, width,
height}], "seed": ...}` with no `created`. The script rebuilds the canonical
envelope: `created` from the local clock, `seed` passed through at the top
level (it is the caller's reproduction handle; the envelope tolerates an
extra scalar), and each image trimmed to the fields that survive the trip --
url or b64_json, width, height, content_type. file_name and file_size
describe a file on fal's side that the caller will never see and are dropped.

The url's own shape decides the conversion, because a 200 can hand back
either form:

  * an http(s) link (the default) goes through as-is, or through
    `rehost_url: true` -- the cross-channel convention the openai, qwen and
    ark scripts read -- which swaps fal's CDN link for one of ours. A
    rehost that finds no object storage keeps the vendor link, exactly as
    ark does; the caller held a working link before the option existed.
  * a data URI (sync_mode) is decoded once:
      - a caller who asked for b64_json gets the bare base64 -- no data
        URI is ever parked in `url`, which is not a URL;
      - a caller who asked for url gets the bytes re-stored
        (`upload_temp_image`), and a deployment without object storage
        degrades that one image to b64_json rather than dressing a data
        URI up as a link.
  * a caller who asked for b64_json but was handed an http link has the
    link fetched and encoded here (`ctx.image_b64`), so the promised shape
    arrives whatever the wire carried.

`response_format` is parked per request id and consumed exactly once by the
reply, the same pocket the tencent_ci script uses; the entry is popped, so a
finished request leaves nothing behind.

Known bounds
------------
  * The 4k tier is documented by fal as "can take several minutes". The
    deployment's UPSTREAM_TIMEOUT (360s in production) caps one upstream
    call and `ctx.emit(timeout=...)` is clamped to the remaining budget, so
    4k is a coin flip against the clock: 1k and 2k are the tiers this
    channel is sized for. Nothing here refuses 4k -- an operator who wants
    it barred should gate it in the routing layer, where the decision is
    visible.
  * Reference images at the edit twin must be >=256px per side and <=4
    megapixels; oversized input is fal's 4xx to give. No preflight, no
    silent downscale -- unlike tencent_ci, nothing here has measured that
    fal's wording for it is misleading, and a conversion the caller never
    asked for is a picture that is no longer theirs.
  * fal's server-side fetch of a reference URL has no documented timeout.
    If one is ever measured (the way ark's 5s was), the answer is an
    `image_ref_mode: "data_uri"` option, and it will be added on evidence,
    not on suspicion.

Provenance: `@v1` is the first and only version. Facts in
`capabilities/fal.json` come from fal's published API pages (checked
2026-10-09) and are marked unverified-by-traffic until a live probe says
otherwise; this file implements the documented contract and the unit tests
pin it against fabrications, never against the billable path.
"""

import base64
import binascii
import time
from functools import partial
from urllib.parse import urlparse

#: fal's own aspect-ratio enum, in document order. `auto` is deliberately
#: absent: it means "derive from the first reference image", which is a
#: decision for the caller to spell, never something a size derivation may
#: produce.
ASPECT_RATIOS = (
    "21:9", "2:1", "16:9", "3:2", "7:5", "4:3", "5:4",
    "1:1", "4:5", "3:4", "5:7", "2:3", "9:16", "1:2",
)

#: Resolution tiers by the long edge they are worth. fal picks the exact
#: canvas inside a tier; the derivation only needs to land in the right one.
#: 512sq is absent on purpose: fal's docs list it, but the live endpoint
#: rejects it with a body-level 422 `input_value_error` (2026-10-09 probe;
#: 768sq, 1k, 2k, 4k all confirmed working). The smallest useful tier is
#: therefore 768sq, and a size under it derives there -- as does an explicit
#: caller `512sq`, which is silently lifted rather than forwarded to a
#: rejection the caller cannot read (the engine carries no upstream body).
RESOLUTION_CEILINGS = (
    (768, "768sq"),
    (1536, "1k"),
    (3072, "2k"),
)

MAX_EDIT_IMAGES = 10

#: The optional knobs forwarded verbatim when the caller supplies them.
PASSTHROUGH = (
    "safety_tolerance",
    "enable_prompt_expansion",
    "output_format",
    "version",
)

#: The suffix pair fal uses to split an app by operation.
T2I_SUFFIX = "text-to-image"
EDIT_SUFFIX = "edit-image"

#: Reply items keep only what survives the trip.
ITEM_FIELDS = ("width", "height", "content_type")

MIME_EXT = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/webp": "webp"}

#: `response_format` parked per request id, consumed once by the reply.
_STATE = {}


def _parse_size(size):
    """A canonical "WxH" as a pixel pair, or None when it does not parse.

    None is the honest return for junk: the caller decorated the request
    with a size nobody can act on, and fal's own defaults are a working
    answer. Zero or negative edges read the same way -- no tier or ratio
    can be derived from them, and refusing the whole request over one
    decorative field is a trade this channel does not make.
    """
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


def _nearest_ratio(width, height):
    """The enum neighbour of w/h, ties resolved by document order."""
    target = width / height

    def distance(ratio):
        left, _, right = ratio.partition(":")
        return abs(int(left) / int(right) - target)

    return min(ASPECT_RATIOS, key=distance)


def _resolution_of(width, height):
    """The tier whose long-edge ceiling first fits, else the 4k overflow."""
    edge = max(width, height)
    for ceiling, tier in RESOLUTION_CEILINGS:
        if edge <= ceiling:
            return tier
    return "4k"


def _endpoint(ctx, edit):
    """The fal endpoint id this request goes to, on the channel's own host.

    The channel URL is the source of truth for scheme and host; only the
    path's operation suffix is decided here. A mapped model IS the fal
    endpoint id the caller was routed to, so it is used verbatim on the
    generate side; on the edit side a mapped id carrying neither suffix has
    no twin this script can name, and the request fails rather than
    fabricating a path -- see "Two endpoints, one script" above.
    """
    parsed = urlparse(ctx.upstream_url)

    def assembled(parts):
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}/{'/'.join(parts)}"

    if ctx.mapped_model:
        segments = [s for s in str(ctx.mapped_model).split("/") if s]
        last = segments[-1] if segments else None
        if last in (T2I_SUFFIX, EDIT_SUFFIX):
            if edit:
                segments[-1] = EDIT_SUFFIX
        elif edit:
            ctx.fail(
                f"X-Model-Map endpoint {ctx.mapped_model!r} has no fal edit twin "
                f"(its path ends in neither {T2I_SUFFIX} nor {EDIT_SUFFIX})",
                code="channel_config_error",
            )
        return assembled(segments)

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        ctx.fail(
            "X-Upstream-Url must carry the fal endpoint path "
            f"(e.g. https://fal.run/blackforestlabs/flux-3/{T2I_SUFFIX})",
            code="channel_config_error",
        )
    suffix = EDIT_SUFFIX if edit else T2I_SUFFIX
    if segments[-1] in (T2I_SUFFIX, EDIT_SUFFIX):
        segments[-1] = suffix
    else:
        segments.append(suffix)
    return assembled(segments)


def _decode_data_uri(ctx, uri):
    """A data URI as (mime, bytes); an unusable one fails the request.

    This is the upstream's own answer being malformed -- a fact about fal,
    not about the caller's image -- so it surfaces as an upstream-shaped
    failure rather than being silently passed through as a "url".
    """
    head, sep, payload = uri.partition(",")
    if not sep:
        ctx.fail("upstream returned an unusable data URI (no payload)")
    mime = head[len("data:"):].split(";", 1)[0] or "image/png"
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        ctx.fail("upstream returned an unusable data URI (not base64)")
    if not raw:
        ctx.fail("upstream returned an empty data URI")
    return mime, raw


async def _inline_item(ctx, uri, want_url):
    """One sync_mode answer: bytes out of the data URI, shape per the ask.

    `want_url` re-stores the bytes so the caller holds a link; a deployment
    with no object storage degrades that image to b64_json instead of
    dressing a data URI up as a link. Without `want_url` the bare base64 is
    the answer directly.
    """
    mime, raw = _decode_data_uri(ctx, uri)
    if not want_url:
        return {"b64_json": ctx.encode_b64(raw)}
    stored = await ctx.upload_temp_image(raw, ext=MIME_EXT.get(mime, "png"))
    if ctx.is_url(stored):
        return {"url": stored}
    return {"b64_json": ctx.encode_b64(raw)}


async def _reply_item(ctx, requested, entry):
    """One fal ImageFile as a canonical data[] item.

    The url's shape decides the conversion -- see "What the reply owes" --
    and an entry that fits no known shape rides through untouched: this
    channel's job is to translate what fal documented, not to second-guess
    what it actually sent.
    """
    if not isinstance(entry, dict):
        return entry
    item = {key: entry[key] for key in ITEM_FIELDS if entry.get(key) is not None}
    url = entry.get("url")
    if isinstance(url, str) and url.startswith("data:"):
        item.update(await _inline_item(ctx, url, want_url=requested != "b64_json"))
        return item
    if isinstance(url, str) and url.startswith("http"):
        if requested == "b64_json":
            item["b64_json"] = await ctx.image_b64(url)
            return item
        if ctx.options.get("rehost_url") is True:
            stored = await ctx.rehost_image(url)
            item["url"] = stored if stored is not None else url
            return item
        item["url"] = url
        return item
    b64 = entry.get("b64_json")
    if isinstance(b64, str) and b64:
        item["b64_json"] = b64
        return item
    return entry


async def _response(ctx, payload):
    """fal's `{"images": [...]}` as the canonical envelope."""
    images = payload.get("images")
    if not isinstance(images, list) or not images:
        # A picture-less 200 is answered loudly: a silent empty `data` would
        # look like success to every caller that only checks the status.
        ctx.fail("upstream returned no images", code="upstream_invalid_response")
    requested = _STATE.pop(ctx.request_id, None)
    items = await ctx.fanout(images, partial(_reply_item, ctx, requested))
    out = {"created": int(time.time()), "data": items}
    seed = payload.get("seed")
    if seed is not None:
        out["seed"] = seed
    return out


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = payload.get("response_format")
    if requested == "url" or requested == "b64_json":
        _STATE[ctx.request_id] = requested

    prompt = payload.get("prompt", "")
    image = payload.get("image")
    refs = image if isinstance(image, list) else ([image] if image else [])
    edit = bool(refs)

    if edit and not str(prompt).strip():
        # The edit twin documents prompt as required. Refusing here names
        # the field for the caller instead of spending the request on fal's
        # 422, which says the same thing less usefully.
        ctx.fail("fal edit-image requires a prompt", param="prompt")
    if len(refs) > MAX_EDIT_IMAGES:
        ctx.fail(
            f"fal edit-image accepts at most {MAX_EDIT_IMAGES} reference images, "
            f"got {len(refs)}",
            param="image",
        )

    body = {"prompt": prompt}

    aspect = payload.get("aspect_ratio")
    resolution = payload.get("resolution")
    if resolution == "512sq":
        # Documented but rejected live (see RESOLUTION_CEILINGS): lifted to
        # the smallest tier that actually runs, keeping the caller's intent
        # -- "smallest you have" -- intact.
        resolution = "768sq"
    if aspect is None or resolution is None:
        pixels = _parse_size(payload.get("size"))
        if pixels is not None:
            if aspect is None:
                aspect = _nearest_ratio(*pixels)
            if resolution is None:
                resolution = _resolution_of(*pixels)
    # A non-string explicit value is a typo, dropped rather than forwarded:
    # an int in an enum slot is fal's 422 either way, and the request is
    # more useful spent on the prompt the caller actually wrote.
    if isinstance(aspect, str) and aspect:
        body["aspect_ratio"] = aspect
    if isinstance(resolution, str) and resolution:
        body["resolution"] = resolution

    if requested == "b64_json":
        # The canonical promise outranks an explicit sync_mode=false: a
        # caller who asked for bytes cannot be answered with a CDN link.
        body["sync_mode"] = True
    elif payload.get("sync_mode") is not None:
        body["sync_mode"] = payload["sync_mode"]

    for key in PASSTHROUGH:
        value = payload.get(key)
        if value is not None:
            body[key] = value

    if edit:
        body["image_urls"] = refs

    ctx.emit(url=_endpoint(ctx, edit))
    return body
