"""volcengine_ark/images@v1: doubao-seedream text-to-image and image-to-image.

Channel setup (New API side):
  X-Upstream-Url: https://ark.cn-beijing.volces.com/api/v3/images/generations
  X-Script-Ref:   volcengine_ark/images@v1
  Authorization:  Bearer <ARK_API_KEY>
  X-Channel-Options: {"model": "doubao-seedream-5-0-260128"}  (optional)

Seedream exposes editing through this same endpoint by accepting an `image`
field, so image-to-image needs no separate route and no separate script: when
the caller sends `image`, it is forwarded and the model edits instead of
generating.

Known constraints (verified 2026-09-04):
  - Minimum pixel count 3686400: 1024x1024 is refused, so small OpenAI sizes
    are upgraded to the "2k" preset.
  - Size presets must be lowercase: 2k / 3k / 4k.
  - `image` takes a public URL or bare base64; a data URI is rejected, so a
    data URI from the client is unwrapped before it is forwarded.
  - The response is already OpenAI-shaped; only format mismatches need work.
"""

SMALL_SIZES = {"1024x1024", "1024x1792", "1792x1024", "512x512", "256x256"}
MID_SIZES = {"2048x2048", "2048x2560", "2560x2048"}
LARGE_SIZES = {"3840x2160", "2160x3840", "3072x3072", "4096x4096"}

# Vendor-specific knobs a caller may pass straight through.
PASSTHROUGH = ("seed", "guidance_scale", "sequential_image_generation_options")


def _ark_size(size):
    if size in SMALL_SIZES or size in MID_SIZES:
        return "2k"
    if size in LARGE_SIZES:
        return "4k"
    return size


async def _to_ark_ref(ctx, ref):
    """ARK accepts a URL or bare base64, but not a data URI."""
    ref = ref.strip()
    if ctx.is_url(ref):
        return ref
    # Handles both data URIs and bare base64, and validates the encoding.
    return await ctx.image_b64(ref)


async def transform(ctx, payload, phase):
    if phase == "request":
        body = {
            "model": ctx.options.get("model", "doubao-seedream-5-0-260128"),
            "prompt": payload.get("prompt", ""),
            "size": _ark_size(payload.get("size", "1024x1024")),
            "response_format": payload.get("response_format", "url"),
            "sequential_image_generation": "disabled",
            "watermark": ctx.options.get("watermark", True),
            "stream": False,
        }

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

    # response phase: ark already returns {"data": [...], "created": ...}
    return {
        "created": payload.get("created", 0),
        "data": payload.get("data", []),
    }
