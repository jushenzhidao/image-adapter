"""The four client entries onto one canonical image request.

`/v1/images/generations` is the canonical contract; `/v1/images/edits` has
always been a thin front door onto it (see ``adapter/api/image_edits.py``).
chat and responses were not, so an image channel reached through them sent an
**empty prompt** -- the script reads `prompt` while a chat body only carries
`messages` -- and the vendor answered with a protobuf complaint about an
uninitialised oneof:

    contents[0].parts[0].data: required oneof field 'data' must have one
    initialized field

That message names neither `prompt` nor "empty", which is why it looked like a
spelling bug and cost a debugging session. This module closes the gap.

It owns the translation in both directions and nothing else: pure functions over
dicts, no I/O, no framework, no vendor knowledge. Which fields a door
contributes is that door's business; the script never learns which one it came
through.

Two rules decide the shape of everything below.

**Rebuild, never patch.** The body handed to a script must carry canonical
fields only. ``openai/images@v1`` forwards the client body verbatim on its
text-to-image path (``return dict(payload)``), so a surviving ``messages`` would
reach the vendor; a chat-shaped ``response_format`` -- ``{"type": ...}``, which
means *structured output*, not *url or b64_json* -- would fail canonical
validation as well. So these functions build a fresh dict and drop the rest,
including ``messages`` / ``input``: precisely what used to arrive as an empty
prompt.

**Answer in the door's shape.** A canonical reply is a list of images; a chat
client wants ``choices[0].message.content``, a responses client wants
``output[]``. Both wrappers step aside when a script already produced the native
shape, so a channel whose script speaks chat or responses itself keeps working
rather than being wrapped twice.

One deliberate omission: ``stream`` is never folded into the canonical body. It
is an entry-level switch (the handler decides SSE), and a script that forwards
the client body verbatim would otherwise hand it to the vendor. The chat route
reads it from the original body instead.
"""

from __future__ import annotations

import base64
import binascii
import time
import uuid

from adapter.errors import InvalidRequestError

#: Roles whose text is an instruction rather than the request itself.
#: `developer` is OpenAI's newer spelling of `system`.
_INSTRUCTION_ROLES = frozenset({"system", "developer"})

#: Part types that carry text, and those that carry an image, across the whole
#: OpenAI family. chat spells them `text` / `image_url`; responses spells the
#: same two things `input_text` / `input_image`. Accepting either spelling on
#: either door costs nothing and spares every caller from knowing which one its
#: SDK picked.
_TEXT_PART_TYPES = frozenset({"text", "input_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image", "image"})

_UNSUPPORTED = "unsupported_parameter"

#: Magic numbers, for labelling the base64 branch. A canonical `b64_json` item
#: carries no mime type, and a `data:` URI with the wrong one is worse than no
#: data URI at all -- vendors return JPEG as readily as PNG.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

#: Keys a wrapper consumed itself; everything else a script added travels on.
_HANDLED_CHAT = frozenset({"created", "data"})
_HANDLED_RESPONSE = frozenset({"created", "data", "id", "object", "status", "output"})


def _mime_of(blob: str) -> str:
    """Mime of an encoded image, from its first bytes only.

    Thirty-two base64 characters decode to more bytes than every magic number
    in ``_MAGIC`` needs, so a multi-megabyte payload is never decoded just to be
    labelled. Anything unrecognised is reported as PNG, the same default the
    scripts use when a vendor omits the type.
    """
    head = blob[:32]
    head += "=" * (-len(head) % 4)
    try:
        raw = base64.b64decode(head)
    except (binascii.Error, ValueError):
        return "image/png"
    for magic, mime in _MAGIC:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_ref(part: dict, where: str) -> str:
    """The reference out of an image part, whichever way it is spelled.

    `image_url` is `{"url": ...}` in chat and a bare string in responses, and
    some clients send the bare string on both.
    """
    value = part.get("image_url", part.get("image"))
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(
            f"'{where}.image_url' must be a URL, a data URI or a base64 string",
            param=f"{where}.image_url",
        )
    return value.strip()


def _read_part(part: object, spot: str) -> tuple[str, str | None]:
    """One content part -> (text, image reference); exactly one is non-empty.

    An unknown part type is refused rather than skipped: silently dropping a
    part the caller believes it sent changes the request behind its back, which
    is the same decision `mask` already gets on the image endpoints.
    """
    if isinstance(part, str):
        return part, None
    if not isinstance(part, dict):
        raise InvalidRequestError(f"'{spot}' must be an object", param=spot)
    kind = part.get("type")
    if kind in _TEXT_PART_TYPES:
        text = part.get("text")
        return (str(text) if text is not None else ""), None
    if kind in _IMAGE_PART_TYPES:
        return "", _image_ref(part, spot)
    raise InvalidRequestError(
        f"'{spot}.type' is not a part this channel accepts: {kind!r}",
        param=f"{spot}.type",
        code=_UNSUPPORTED,
    )


def _read_content(content: object, where: str) -> tuple[str, list[str]]:
    """One message's content -> (text, image references), in order.

    `where` names the content field rather than the message, so a refusal points
    at the part the caller has to fix -- `messages[0].content[1].type` -- instead
    of at the whole message.
    """
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise InvalidRequestError(
            f"'{where}' must be a string or an array", param=where
        )

    texts: list[str] = []
    refs: list[str] = []
    for index, part in enumerate(content):
        text, ref = _read_part(part, f"{where}[{index}]")
        if text:
            texts.append(text)
        if ref:
            refs.append(ref)
    return "\n".join(texts), refs


def _attach_images(canonical: dict, refs: list[str]) -> None:
    """Folds references in, collapsing a single one to a scalar.

    The canonical body accepts either shape (``image.py`` validates both), and
    a scalar for the common case keeps the script-side reading code simple.
    """
    if refs:
        canonical["image"] = refs if len(refs) > 1 else refs[0]


def messages_to_canonical(body: dict) -> dict:
    """A chat body -> the canonical image request.

    History is truncated to the last `user` turn: the canonical contract has a
    single `prompt` and a single `image`, so an earlier turn has nowhere to go.
    That last user message supplies both the prompt and the image set, and every
    `assistant` turn is ignored -- its images are the *outputs* of earlier
    turns, not inputs to this one.

    The truncation is a deliberate trade, not an oversight. Refusing outright
    was the original behaviour (``docs/06`` 4.6), and the reason still stands: a
    silent truncation changes the image while the caller believes its context
    was honoured, which is a bug that looks like a model problem for weeks. The
    caller opted into truncation over refusal; what the refusal bought was a
    loud failure, and that is what is given up.

    An unknown role is still refused -- a misspelled ``"user"`` must not pass as
    history and quietly reduce the request to the wrong turn.
    """
    messages = body.get("messages")
    if not messages or not isinstance(messages, list):
        raise InvalidRequestError(
            "'messages' must be a non-empty list", param="messages"
        )

    instructions: list[str] = []
    instruction_refs: list[str] = []
    user_text = ""
    user_refs: list[str] = []
    users = 0

    for index, message in enumerate(messages):
        where = f"messages[{index}]"
        if not isinstance(message, dict):
            raise InvalidRequestError(f"'{where}' must be an object", param=where)
        role = message.get("role")
        if role in _INSTRUCTION_ROLES:
            text, found = _read_content(message.get("content"), f"{where}.content")
            if text:
                instructions.append(text)
            # A system turn's images are global reference rather than one
            # turn's input, so they survive the truncation below.
            instruction_refs.extend(found)
        elif role == "user":
            users += 1
            # Last one wins, images included: the canonical contract carries a
            # single prompt and a single image set, so an earlier user turn is
            # history and only the newest instruction is actionable.
            user_text, found = _read_content(message.get("content"), f"{where}.content")
            user_refs = list(found)
        elif role == "assistant":
            # Its images are the outputs of earlier turns, not inputs to this
            # one, and its text is not an instruction this channel can act on.
            continue
        else:
            raise InvalidRequestError(
                "This channel turns a single user turn into one image; "
                f"'{where}.role' is {role!r}.",
                param=f"{where}.role",
                code=_UNSUPPORTED,
            )

    if users == 0:
        raise InvalidRequestError(
            "'messages' must contain a user message", param="messages"
        )

    prompt = user_text
    if instructions:
        prompt = "\n".join(instructions) + "\n\n" + user_text

    canonical: dict = {}
    if body.get("model") is not None:
        canonical["model"] = body["model"]
    canonical["prompt"] = prompt
    _attach_images(canonical, instruction_refs + user_refs)
    return canonical


def input_to_canonical(body: dict, prior: object = None) -> dict:
    """A responses body -> the canonical image request.

    `input` arrives as a string or as an array of items, and the items are
    spelled three different ways in the wild: bare strings, `{type: input_text |
    input_image}` parts, or `{role, content}` messages.

    History is truncated to the last user turn, exactly as on the chat door: the
    canonical contract carries a single `prompt` and a single `image`, so an
    earlier turn has nowhere to go. `assistant` items are skipped as history and
    an unknown role is still refused, for the same reason as there.

    The unit of truncation is the *turn*, not the item. `input[]` spells one
    turn two ways -- a `{role, content}` message, or a bare run of
    `{type: input_text | input_image}` parts -- and a turn's parts belong
    together. Truncating item by item would keep a turn's image and drop its
    instruction, which changes the request rather than shortening it.

    `tools` is carried, unlike on the chat door, because here it means
    `image_generation` orchestration -- a field scripts already read to decide
    their response modalities. `_previous_ctx` is the adapter's own state
    hand-off (``resp_ctx:*``); a rebuild that dropped it would silently break
    the state chain.
    """
    raw = body.get("input")
    instructions: list[str] = []
    instruction_refs: list[str] = []
    #: Banked turns, each as (texts, refs). Only the last one survives.
    turns: list[tuple[list[str], list[str]]] = []
    texts: list[str] = []
    refs: list[str] = []
    opened = False

    def close_turn() -> None:
        """Bank the open turn when it carries anything, then clear it."""
        nonlocal opened
        if opened and (texts or refs):
            turns.append((list(texts), list(refs)))
        texts.clear()
        refs.clear()
        opened = False

    def start_turn(*, fresh: bool) -> None:
        """Open a turn, closing the previous one first when `fresh`."""
        nonlocal opened
        if fresh:
            close_turn()
        opened = True

    if isinstance(raw, str):
        start_turn(fresh=False)
        texts.append(raw)
    elif isinstance(raw, list):
        for index, item in enumerate(raw):
            where = f"input[{index}]"
            if isinstance(item, str):
                start_turn(fresh=False)
                texts.append(item)
                continue
            if not isinstance(item, dict):
                raise InvalidRequestError(f"'{where}' must be an object", param=where)
            if "content" in item:
                role = item.get("role")
                if role in _INSTRUCTION_ROLES:
                    # An instruction is not a turn: it applies to whichever turn
                    # survives truncation, so it is never closed by it.
                    chunk, found = _read_content(item.get("content"), f"{where}.content")
                    if chunk:
                        instructions.append(chunk)
                    instruction_refs.extend(found)
                    continue
                if role == "assistant":
                    # History: its images are the outputs of earlier turns, and
                    # its text is not an instruction this channel can act on.
                    close_turn()
                    continue
                if role not in (None, "user"):
                    raise InvalidRequestError(
                        "This channel turns a single user turn into one image; "
                        f"'{where}.role' is {role!r}.",
                        param=f"{where}.role",
                        code=_UNSUPPORTED,
                    )
                start_turn(fresh=True)
                chunk, found = _read_content(item.get("content"), f"{where}.content")
            elif item.get("type") in _TEXT_PART_TYPES | _IMAGE_PART_TYPES:
                # The item *is* the part, so it is named directly rather than
                # through a one-element list: `input[0].type`, not
                # `input[0][0].type`. A bare run of these is one turn's parts,
                # which is why they extend the open turn instead of replacing it.
                start_turn(fresh=False)
                chunk, ref = _read_part(item, where)
                found = [ref] if ref else []
            else:
                raise InvalidRequestError(
                    f"'{where}.type' is not an item this channel accepts: "
                    f"{item.get('type')!r}",
                    param=f"{where}.type",
                    code=_UNSUPPORTED,
                )
            if chunk:
                texts.append(chunk)
            refs.extend(found)
    else:
        raise InvalidRequestError(
            "'input' must be a non-empty string or an array", param="input"
        )

    close_turn()
    last_texts, last_refs = turns[-1] if turns else ([], [])
    text = "\n".join(last_texts)
    refs = instruction_refs + last_refs

    prompt = text
    if instructions:
        prompt = "\n".join(instructions) + "\n\n" + text

    canonical: dict = {}
    if body.get("model") is not None:
        canonical["model"] = body["model"]
    canonical["prompt"] = prompt
    _attach_images(canonical, refs)
    if body.get("tools") is not None:
        canonical["tools"] = body["tools"]
    if prior is not None:
        canonical["_previous_ctx"] = prior
    return canonical


def _image_items(payload: object) -> list | None:
    """The canonical `data[]` list, or None when this is not a canonical reply.

    None is the "not mine" answer, not an error: a script is free to answer in
    the door's native shape instead, and guessing at anything unrecognised would
    corrupt a working channel.
    """
    if not isinstance(payload, dict):
        return None
    items = payload.get("data")
    return items if isinstance(items, list) else None


def _carry_over(out: dict, payload: dict, handled: frozenset[str]) -> None:
    """Keep every key the wrapper did not consume.

    `usage` is billing-relevant, and `gemini_usage` exists so a control plane
    can reconcile against what the vendor actually reported; dropping either
    would turn a successful request into an unbillable one. Whatever else a
    script added travels the same way.
    """
    for key, value in payload.items():
        if key not in handled and value is not None:
            out.setdefault(key, value)


def canonical_to_chat(payload: object, model: str | None) -> object:
    """A canonical reply -> a ChatCompletion, or the payload unchanged.

    The image rides in `content` as a part, the shape the client sends on the
    way in. `b64_json` becomes a data URI, since that is the only way a chat
    content part can carry bytes.
    """
    if isinstance(payload, dict) and "choices" in payload:
        return payload
    items = _image_items(payload)
    if items is None:
        return payload

    parts = []
    for item in items:
        part = _chat_part(item)
        if part:
            parts.append(part)

    out = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": payload.get("created") or int(time.time()),
        "model": model or "",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": parts},
                "finish_reason": "stop",
            }
        ],
    }
    _carry_over(out, payload, _HANDLED_CHAT)
    return out


def _chat_part(item: object) -> dict | None:
    """One canonical `data[]` item as a chat content part.

    `url` and `b64_json` are the two carriers the canonical contract defines.
    An item carrying neither is skipped rather than dressed up: inventing a part
    would misreport what the vendor returned.
    """
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    if isinstance(url, str) and url:
        return {"type": "image_url", "image_url": {"url": url}}
    blob = item.get("b64_json")
    if isinstance(blob, str) and blob:
        mime = _mime_of(blob)
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{blob}"}}
    return None


def canonical_to_response(payload: object, model: str | None) -> object:
    """A canonical reply -> a responses payload, or the payload unchanged.

    `output` carries one `image_generation_call` per image. A single image
    script produces no assistant text and the canonical contract has no field to
    carry any, so no `message` item is invented: an empty one would satisfy the
    letter of AC-08 without telling the caller anything. The capability that
    criterion describes -- a text model rewriting the prompt before the image
    model runs -- belongs to a staged script, and lands in `output` on its own
    when one is configured.

    `result` is base64 when the canonical item has it, otherwise the URL: the
    canonical contract is allowed to answer with either, and a result field is
    more useful than none.
    """
    if isinstance(payload, dict) and "output" in payload:
        return payload
    items = _image_items(payload)
    if items is None:
        return payload

    calls = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result = item.get("b64_json") or item.get("url")
        if not isinstance(result, str) or not result:
            continue
        calls.append(
            {
                "type": "image_generation_call",
                "id": f"ig_{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "result": result,
            }
        )

    out = {
        # `resp-` (not OpenAI's `resp_`) matches the id this route already hands
        # out when a script supplies none, so one endpoint keeps one format.
        "id": f"resp-{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": "completed",
        "created": payload.get("created") or int(time.time()),
        "model": model or "",
        "output": calls,
    }
    _carry_over(out, payload, _HANDLED_RESPONSE)
    return out
