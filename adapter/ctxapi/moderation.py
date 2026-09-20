"""Content-moderation refusals, recognised and reported in one place.

Every image vendor refuses on safety grounds, and every one spells it differently:
qwen answers 200 with an error frame carrying ``code: "data_inspection_failed"``
(plus ``modality``/``stage``/``details``); Gemini answers 200 with
``promptFeedback.blockReason`` or a ``finishReason`` from a small closed set. What
must **not** differ between channels is what the client sees, because whoever sits
downstream keys its retry policy off the status code -- ``docs/06`` puts it as
"这类错误不可重试（同 prompt 必再被拦），控制面应据此停止重试 —— 这正是要用 400
而不是 500 的原因". A refusal can never succeed on a repeat, so it is a **400
``content_filter``**, and that decision lives here rather than in three scripts.

Two layers, deliberately split:

  * **Recognition** -- the natural-language wordings that give a refusal away are
    *shared across every channel* and live in data (``capabilities/_shared.json``),
    so calibrating one is an operator action, not a script edit plus a manifest
    re-hash plus a release. Vendor-specific markers (qwen's ``data_inspection_failed``,
    Gemini's ``finishReason`` values) stay with the vendor: scripts pass them in via
    ``codes=`` or keep their own table, because inventing a shared list of vendor
    codes is exactly the guesswork this split exists to avoid.
  * **The exit contract** -- status, ``code``, ``err_type``, ``param`` and the trace
    note are fixed in ``fail_moderation``, so all channels answer alike.

The measured defaults below are a **deliberate exception** to "one source per fact"
(``adapter/capabilities.py`` header). Losing a capability table means "send less";
losing *this* one would mean a refusal is classified as a retryable upstream hiccup
-- the same bug, on every channel, silently. So an absent table degrades to these
literals rather than to silence, and the literals cite where they came from.
"""

from __future__ import annotations

from typing import NoReturn

from adapter.capabilities import shared_facts
from adapter.ctxapi.base import CtxMixin

#: Wordings measured from a real refusal (2026-09-19). qwen's frame:
#: ``{"error": {"code": "data_inspection_failed", "modality": ["text"],
#: "stage": "input", "details": "内容安全警告：输入数据可能包含不适当的内容！"}}``
#: They are ordinary Chinese phrasing rather than a vendor's private string, which
#: is why they belong to every channel and not to qwen's script.
MODERATION_WORDINGS = ("内容安全警告", "不适当的内容")

#: One canonical suffix, so "do not retry" reaches the caller in the same words on
#: every channel. The vendor keeps its own opening clause (Gemini names Gemini).
NO_RETRY_SUFFIX = "（同 prompt 重试必再被拒，请改提示词或换素材）"

#: How far into a payload the matcher looks. Refusals arrive shallow (an error
#: object, or a message with a reason); walking deeper only widens the chance of
#: matching an echo of the caller's own prompt.
_MAX_DEPTH = 3
_MAX_STRINGS = 40
_MAX_TOTAL_CHARS = 4000


def _blob(payload: object, *, depth: int = 0, out: list[str] | None = None) -> str:
    """Every string a payload carries, bounded, as one searchable blob.

    Bounded on purpose: a vendor response can be megabytes of base64, and a
    substring scan over that is neither cheap nor meaningful.
    """
    out = out if out is not None else []
    if len(out) >= _MAX_STRINGS or depth > _MAX_DEPTH:
        return " ".join(out)[:_MAX_TOTAL_CHARS]
    if isinstance(payload, (str, bytes, bytearray)):
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8", "replace")
        if payload:
            out.append(payload[:1000])
    elif isinstance(payload, dict):
        for value in payload.values():
            _blob(value, depth=depth + 1, out=out)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _blob(value, depth=depth + 1, out=out)
    return " ".join(out)[:_MAX_TOTAL_CHARS]


class ModerationMixin(CtxMixin):
    """Recognise a safety refusal and answer it the same way on every channel."""

    def matched_moderation(
        self,
        payload: object,
        *,
        codes: tuple[str, ...] | list[str] = (),
        wordings: tuple[str, ...] | list[str] = (),
    ) -> str:
        """The token that marks `payload` as a moderation refusal, or ``""``.

        Looks for vendor markers (``codes``, plus ``wordings`` a script knows) and
        for the shared wordings from ``capabilities/_shared.json``. Returns the
        matched token so a caller can quote it in its message instead of naming a
        blob it did not really inspect.

        False positives are accepted on purpose: the cost of reading a genuine
        refusal as retryable (a wasted generation, and a client told "later") is
        worse than reading one odd error as a refusal.
        """
        blob = _blob(payload)
        if not blob:
            return ""
        tokens: list[str] = [*(codes or ()), *(wordings or ())]
        tokens.extend(self._shared_wordings())
        tokens.extend(MODERATION_WORDINGS)
        for token in tokens:
            if token and isinstance(token, str) and token in blob:
                return token
        return ""

    def fail_moderation(
        self, message: str, *, param: str = "prompt", **note_extra: object
    ) -> NoReturn:
        """End the request with the canonical content-refusal error.

        **400 ``content_filter``** with ``err_type=invalid_request_error`` -- a 4xx,
        because a control plane that retries 5xx must not retry this. `note_extra`
        is appended to the trace note for vendor facts worth keeping (qwen puts its
        ``modality``/``stage``/``details`` there; "the prompt was refused" and "the
        picture was refused" need different advice).
        """
        shown = str(message)[:200]
        try:
            self.logfire.info("moderation refusal", outcome="content_refused",
                              param=param, detail=shown, **note_extra)
        except Exception:  # noqa: BLE001 - instrumentation must not fail a request
            pass
        self.fail(shown + NO_RETRY_SUFFIX, code="content_filter", param=param,
                  err_type="invalid_request_error", status=400)

    def _shared_wordings(self) -> tuple[str, ...]:
        """`capabilities/_shared.json`'s `moderation_wordings`, filtered to strings."""
        try:
            table = shared_facts(self.settings.capability_roots)
        except Exception:  # noqa: BLE001 - a data file must never break a request
            return ()
        raw = (table or {}).get("moderation_wordings")
        if not isinstance(raw, list):
            return ()
        return tuple(item for item in raw if isinstance(item, str) and item)
