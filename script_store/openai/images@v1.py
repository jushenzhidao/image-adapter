"""openai/images@v1: OpenAI-native generations (JSON) and edits (multipart).

Per-image work runs through ``ctx.fanout``, so N references cost one wait
instead of N -- the client's references on the way in (including a mask), and
the N reply items on the way back. Nothing else is involved: same body, same
multipart part order, same error codes, and N=1 still takes the serial path.

That concurrency is a *behaviour* the caller can observe: a multi-reference edit
puts up to ``fanout_concurrency`` concurrent requests on the caller's image
origin instead of one at a time, and the same for the N conversions on the way
back. What it does not change is the request the upstream receives.

Channel setup (New API side):
  X-Upstream-Url:    https://api.openai.com/v1/images/generations
  X-Script-Ref:      openai/images@v1
  Authorization:     Bearer <OPENAI_API_KEY>
  X-Channel-Options: {"edits_url": "..."}    (optional, see below)

OpenAI splits one task across two endpoints by transport rather than by
semantics: /v1/images/generations takes JSON, /v1/images/edits takes
multipart/form-data with an `image` file part. The adapter's canonical body
already carries that distinction in a single field, so this script picks the
endpoint and the encoding from `payload["image"]` alone:

    no image    POST  {channel url}     application/json
    image       POST  {sibling /edits}  multipart/form-data

X-Upstream-Url therefore names the **generations** endpoint: it is the primary
one, the only URL the channel has to declare, and the one a text-to-image
request uses unchanged. The edits endpoint is derived as its sibling -- same
scheme, host and query, trailing path segment swapped -- which keeps the
"one channel = one upstream endpoint" rule while the script handles the
second URL the same way it handles every other vendor difference.

A service whose layout does not put the two endpoints side by side (Azure
OpenAI deployments, for instance) names the sibling outright:

    X-Channel-Options: {"edits_url": "https://..../images/edits?api-version=..."}

Both endpoints answer with the OpenAI images envelope -- `{created, data}`,
optionally `usage` -- and usage is forwarded whatever the shape of the rest,
because it is what billing is computed from.

Output shape
------------
OpenAI-compatible gateways are not equally literal about `response_format`.
Some answer `"url"` with a real link; others hand back a `data:` URI inside
`data[].url`; others ignore the field and return `b64_json` regardless. Rather
than passing that inconsistency on to the caller, this script delivers the
shape that was asked for:

    asked for url       a data URI or b64_json is stored and returned as a link
    asked for b64_json  a URL is fetched, or a data URI decoded, and encoded

When the caller says nothing the response rides through untouched. The front
door's `response_format` default decides only whether a value is legal; reading
it here as an instruction would route every un-asked-for image through object
storage, which is a far larger change than it looks.

Two things to know before wiring a channel:

  * `response_format="b64_json"` against an upstream that answers with a link
    downloads from that host, so the host has to pass the channel's whitelist
    (`upstream_host_set`), exactly as a client-supplied image URL does.
  * `response_format="url"` needs object storage to produce a *link*. Without
    MinIO that conversion is skipped and the upstream's own shape comes back,
    so the caller can receive `b64_json` -- or the `data:` URI the upstream put
    in `url` -- while having asked for `url`. That is deliberate: the missing
    piece is our configuration, and a 502 raised out of it would fail requests
    that used to work. It does mean a deployment that promises links has to
    have MinIO configured.

Note the helper convention: sync helpers carry annotations, the async ones do
not, matching the other scripts in this store.
"""

from functools import partial
from urllib.parse import urlsplit, urlunsplit

#: Fields that change shape on the way out: they become multipart file parts
#: rather than text. Every other field the caller sends rides along as a text
#: part, so vendor extras survive untouched.
FILE_FIELDS = ("image", "mask")

#: The two shapes one upstream image item can carry the picture in.
CARRIERS = ("url", "b64_json")

#: The response phase cannot see the client's request -- the adapter hands the
#: *upstream* response back to the script -- so the output shape the caller
#: asked for is parked here, keyed by request id, between the two phases. This
#: is the documented way to carry a decision across phases; there is no other.
_STATE: dict[str, str] = {}


def _sibling_edits_url(url: str) -> str:
    """Swaps the trailing path segment for ``edits``, keeping host and query."""
    parts = urlsplit(url)
    head, _, _ = parts.path.rstrip("/").rpartition("/")
    path = f"{head}/edits" if head else "/edits"
    return urlunsplit(parts._replace(path=path))


def _as_text(value) -> str:
    """multipart carries text only, and a JSON ``true`` must read ``true``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _filename(stem: str, mime: str) -> str:
    """A filename whose extension matches the sniffed type, for tidiness."""
    subtype = mime.split("/", 1)[-1].split("+")[0]
    return f"{stem}.{subtype or 'bin'}"


def _requested_format(payload: dict) -> str | None:
    """The output shape the caller asked for, or None when it said nothing."""
    value = payload.get("response_format")
    return value if value in CARRIERS else None


def _with_carrier(item: dict, key: str, value: str) -> dict:
    """One item with its picture moved into the requested carrier.

    Extras such as ``revised_prompt``, ``width`` and ``height`` ride along:
    the request body is a superset on the way in, and the way back has no
    reason to be narrower.
    """
    out = {k: v for k, v in item.items() if k not in CARRIERS}
    out[key] = value
    return out


async def _file_part(ctx, pair):
    """One (reference, stem) pair -> (filename, bytes, content-type).

    Takes a pair rather than two arguments because ``ctx.fanout`` hands its work
    one item at a time; ``functools.partial`` supplies ctx, so what the fan-out
    sees is a single-argument callable.
    """
    ref, stem = pair
    data = await ctx.image_bytes(ref)
    mime = ctx.sniff_mime(data)
    return (_filename(stem, mime), data, mime)


async def _payload_bytes(ctx, item):
    """The picture behind one item, whichever carrier the upstream used."""
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64:
        return ctx.decode_b64(b64)

    url = item.get("url")
    if isinstance(url, str) and url:
        # A data URI already *is* the image: there is nothing to fetch, and the
        # HTTP client would refuse the scheme. Decoding is the whole job.
        if ctx.is_data_uri(url):
            return ctx.decode_b64(url)
        if ctx.is_url(url):
            return await ctx.download_image(url)
    return None


async def _as_url(ctx, item):
    """One item carrying a link when one can be had, else the upstream's own shape.

    Object storage can be absent (dev, or a deployment that never configured
    MinIO). That is our gap, not the caller's mistake, and turning it into a
    502 helps nobody: when the upload cannot produce a real link the upstream's
    answer is passed through untouched -- exactly what this script did before
    it converted anything. What is never done is putting a data URI under
    `url`; if the upstream did that, that is its answer, not one we invented.
    """
    url = item.get("url")
    if isinstance(url, str) and ctx.is_url(url):
        return item

    raw = await _payload_bytes(ctx, item)
    if raw is None:
        ctx.fail(
            "Upstream returned an item with no image payload",
            code="upstream_error",
            status=502,
        )

    mime = ctx.sniff_mime(raw)
    subtype = mime.split("/", 1)[1] if mime.startswith("image/") else "png"
    stored = await ctx.upload_temp_image(raw, ext=subtype)
    if not ctx.is_url(stored):
        # No object storage: upload_temp_image degrades to a data URI, so there
        # is no link to hand back. Pass the item through rather than fail.
        return item
    return _with_carrier(item, "url", stored)


async def _as_b64(ctx, item):
    """One item guaranteed to carry base64."""
    b64 = item.get("b64_json")
    if isinstance(b64, str) and b64:
        return item

    raw = await _payload_bytes(ctx, item)
    if raw is None:
        ctx.fail(
            "Upstream returned an item with no image payload",
            code="upstream_error",
            status=502,
        )
    return _with_carrier(item, "b64_json", ctx.encode_b64(raw))


async def _shape(ctx, payload, want):
    """Rewrites every image item into the shape the caller asked for."""
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        return payload

    convert = _as_url if want == "url" else _as_b64
    # Concurrently. Each item may need a fetch and a store upload, and those are
    # waits rather than work, so N of them cost one wait instead of N. `data`
    # stays in the order the vendor sent whatever order the conversions finish
    # in, and an adapter error raised by any item still reaches the client with
    # its own status: ctx.fanout re-raises the earliest failing item's own
    # exception rather than an ExceptionGroup.
    data = await ctx.fanout(items, partial(_convert_one, ctx, convert))
    return {**payload, "data": data}


async def _convert_one(ctx, convert, item):
    """One reply item: converted when it is an object, passed through if not.

    Module-level with the context and the converter bound by
    ``functools.partial`` instead of closing over them, so ``ctx.fanout``
    receives the single-argument callable it expects.
    """
    if not isinstance(item, dict):
        return item
    return await convert(ctx, item)


async def _response(ctx, payload):
    want = _STATE.pop(ctx.request_id, None)
    if want is None or not isinstance(payload, dict):
        return payload
    return await _shape(ctx, payload, want)


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    requested = _requested_format(payload)
    if requested is not None:
        _STATE[ctx.request_id] = requested

    image = payload.get("image")
    if not image:
        # Text-to-image: the canonical body is already the generations body,
        # and the channel URL already points at that endpoint.
        return dict(payload)

    refs = image if isinstance(image, list) else [image]
    # OpenAI repeats a bracketed field name when a request carries several
    # images and uses the bare name for one -- the same convention the
    # adapter's own /v1/images/edits front door accepts on the way in.
    name = "image[]" if len(refs) > 1 else "image"
    # The mask rides in the same batch as the references: it is an independent
    # read of the same kind, and a slow mask costs exactly as much as a slow
    # reference. The batch is then split back apart, so the part order -- and
    # therefore the multipart body -- is what it always was.
    pairs = [(ref, f"image{i}") for i, ref in enumerate(refs)]
    if payload.get("mask"):
        pairs.append((payload["mask"], "mask"))
    parts = await ctx.fanout(pairs, partial(_file_part, ctx))
    files: dict[str, list] = {name: parts[: len(refs)]}
    if payload.get("mask"):
        files["mask"] = parts[len(refs) :]

    ctx.emit(
        url=ctx.options.get("edits_url") or _sibling_edits_url(ctx.upstream_url),
        files=files,
    )
    return {
        key: _as_text(value)
        for key, value in payload.items()
        if key not in FILE_FIELDS and value is not None
    }
