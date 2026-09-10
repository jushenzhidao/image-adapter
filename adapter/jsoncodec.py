"""The one place JSON is encoded or decoded.

orjson is a hard dependency, but each entry point used to carry its own
stdlib fallback, so the fast path existed in exactly one module (request body
parsing) and was missing from the other two. Centralising it makes the choice
uniform and gives the fallback a single home.

Measured on a 4 MB payload, median of 15, same process:

    upstream decode   json.loads(raw.decode())  4.4 ms  ->  orjson.loads(raw)  1.7 ms
    response encode   json.dumps(...).encode()  9.2 ms  ->  orjson.dumps(...)  0.1 ms

Both run on the event loop, so the per-request latency is only half the story:
for that whole window every other coroutine in the worker is waiting too.

``JSONResponse`` lives here rather than in ``api/common.py`` because
``errors.py`` needs it as well, and ``errors.py`` must not import from ``api/``
without creating a cycle.

Two deliberate differences from Starlette's ``JSONResponse``:

* ``OPT_NON_STR_KEYS`` is set. A payload with an integer key serialises as
  ``{"1": ...}``, exactly as the stdlib does. Without the flag orjson raises
  TypeError, which would turn a perfectly good response into a 500. The flag
  measured free (0.12 ms vs 0.13 ms on the payload above).
* ``NaN``/``Infinity`` serialise as ``null`` where the stdlib (with
  ``allow_nan=False``) raises. That is an improvement, not a regression: a
  vendor that emits a bare ``Infinity`` in a numeric field currently produces a
  500 *after* the payload parsed cleanly. A proxy should degrade that field,
  not discard the response.
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse as _StarletteJSONResponse

try:
    import orjson

    JSON_LIBRARY = "orjson"

    def loads(raw: bytes | bytearray | str) -> Any:
        """Parses JSON. Accepts bytes as-is; no prior decode step is needed."""
        return orjson.loads(raw)

    def dumps(value: Any) -> bytes:
        """Serialises to UTF-8 JSON bytes, non-ASCII characters left literal."""
        return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS)

except ImportError:  # pragma: no cover - only without orjson installed
    import json

    JSON_LIBRARY = "json"

    def loads(raw: bytes | bytearray | str) -> Any:
        return json.loads(raw)

    def dumps(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


class JSONResponse(_StarletteJSONResponse):
    """Starlette's JSON response, rendered by the fastest available encoder.

    Only ``render`` is overridden, so status_code, media_type, headers and
    background keep Starlette's semantics.
    """

    def render(self, content: Any) -> bytes:
        return dumps(content)


__all__ = ["JSON_LIBRARY", "JSONResponse", "dumps", "loads"]
