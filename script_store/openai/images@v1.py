"""openai/images@v1: OpenAI-native generations (JSON) and edits (multipart).

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
optionally `usage` -- so the response phase is a pass-through, usage included
(it is what billing is computed from).
"""

from urllib.parse import urlsplit, urlunsplit

#: Fields that change shape on the way out: they become multipart file parts
#: rather than text. Every other field the caller sends rides along as a text
#: part, so vendor extras survive untouched.
FILE_FIELDS = ("image", "mask")


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


async def _file_part(ctx, ref, stem):
    """One image reference -> (filename, bytes, content-type)."""
    data = await ctx.image_bytes(ref)
    mime = ctx.sniff_mime(data)
    return (_filename(stem, mime), data, mime)


async def transform(ctx, payload, phase):
    if phase != "request":
        return payload

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
    files: dict[str, list] = {
        name: [await _file_part(ctx, ref, f"image{i}") for i, ref in enumerate(refs)]
    }
    if payload.get("mask"):
        files["mask"] = [await _file_part(ctx, payload["mask"], "mask")]

    ctx.emit(
        url=ctx.options.get("edits_url") or _sibling_edits_url(ctx.upstream_url),
        files=files,
    )
    return {
        key: _as_text(value)
        for key, value in payload.items()
        if key not in FILE_FIELDS and value is not None
    }
