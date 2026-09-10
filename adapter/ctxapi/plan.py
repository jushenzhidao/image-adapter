"""``RequestPlan`` and ``ctx.emit()``: how a script steers the outbound call.

A script expresses the request body in one of three shapes, and picking
between them is the only thing here that is not a plain override:

  dict / list        JSON (the default, and the only one a plain ``return``
                     produces)
  ``form=...``       application/x-www-form-urlencoded
  ``files=...``      multipart/form-data, with the returned dict as its text
                     parts and ``files`` as its binary ones

``files`` exists because an OpenAI-native edits endpoint takes an ``image``
file part, so a JSON body cannot express it. The engine owns the encoding --
scripts hand over bytes and a filename and never see a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from adapter.ctxapi.base import CtxMixin
from adapter.urlguard import check_url

#: One binary part: (filename, bytes, content-type).
FilePart = tuple[str, bytes, str]

_BYTES_LIKE = (bytes, bytearray, memoryview)


def _normalise_files(files: dict[str, Any] | None) -> dict[str, list[FilePart]] | None:
    """Accepts one part or a list of parts per field, stores a list always.

    The common case is a single part for a single field, so
    ``files={"image": ("a.png", raw, "image/png")}`` is the ergonomic form; a
    list covers a field that repeats (OpenAI spells a multi-image edit
    ``image[]``). Normalising here keeps the transport a dumb consumer, and
    rejecting non-bytes early means a script bug reads as a clear message
    instead of surfacing later as an opaque aiohttp serialisation error.
    """
    if files is None:
        return None
    normalised: dict[str, list[FilePart]] = {}
    for name, value in files.items():
        parts = value if isinstance(value, list) else [value]
        out: list[FilePart] = []
        for part in parts:
            if not isinstance(part, (tuple, list)) or len(part) != 3:
                raise TypeError(
                    f"files[{name!r}] parts must be (filename, bytes, content_type)"
                )
            filename, data, mime = part
            if not isinstance(data, _BYTES_LIKE):
                raise TypeError(
                    f"files[{name!r}] data must be bytes, got {type(data).__name__}"
                )
            # bytes stays as-is; a bytearray or memoryview is copied once so the
            # stored plan holds a plain, immutable bytes object throughout.
            raw = data if isinstance(data, bytes) else bytes(data)
            out.append((str(filename), raw, str(mime)))
        normalised[str(name)] = out
    return normalised


@dataclass
class RequestPlan:
    """Overrides a script declares for the outbound call via ctx.emit()."""

    url: str | None = None
    method: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_set: bool = False
    form: dict[str, Any] | None = None
    raw: bytes | None = None
    files: dict[str, list[FilePart]] | None = None
    timeout: float | None = None


class PlanMixin(CtxMixin):
    """Declarative outbound-call overrides."""

    plan: RequestPlan

    def reset_plan(self) -> None:
        """Engine-side hook: drops overrides between phases."""
        self.plan = RequestPlan()

    def emit(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        raw: bytes | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Declares outbound-call overrides from the request phase.

        Returning a dict from transform() already sets the JSON body, so
        emit() is only needed for anything beyond it: a sub-path on the
        channel URL, a different method, extra headers, form encoding,
        multipart parts, or raw bytes.

        ``files`` switches the call to multipart/form-data: the dict returned
        by transform() becomes its text parts and ``files`` its binary ones.
        """
        plan = self.plan
        if url is not None:
            plan.url = check_url(url, self.settings, header="ctx.emit(url=...)")
        if method is not None:
            plan.method = method.strip().upper()
        if headers:
            plan.headers.update({str(k): str(v) for k, v in headers.items()})
        if query:
            plan.query.update({str(k): str(v) for k, v in query.items()})
        if body is not None:
            plan.body = body
            plan.body_set = True
        if form is not None:
            plan.form = form
        if raw is not None:
            plan.raw = raw
        if files is not None:
            plan.files = _normalise_files(files)
        if timeout is not None:
            plan.timeout = float(timeout)
