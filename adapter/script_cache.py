"""Compiles verified script text into a callable transform, cached by digest.

Keying on sha256 of the source removes the need for hot reload: a changed
script is a different key, and an unchanged one is a cache hit no matter
which channel sent it.
"""

from __future__ import annotations

import importlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from adapter.errors import SecurityError
from adapter.sandbox import ALLOWED_STDLIB, scan_source
from adapter.script_source import ScriptSource

TRANSFORM_NAME = "transform"


def _guarded_import(
    name: str,
    globals_: Any = None,
    locals_: Any = None,
    fromlist: Any = (),
    level: int = 0,
) -> Any:
    """The only import path a script has.

    The AST scan already rejects non-allowlisted module names, but it cannot
    see through a submodule import such as `import urllib.parse`, and it is
    not the last line of defence: this re-checks at execution time so the
    allowlist holds even if the scanner is bypassed.
    """
    if level != 0:
        raise ImportError("relative imports are not allowed in scripts")
    root = name.split(".", 1)[0]
    if name not in ALLOWED_STDLIB and root not in ALLOWED_STDLIB:
        raise ImportError(f"import of {name!r} is not allowed in scripts")
    return importlib.__import__(name, globals_, locals_, fromlist, level)

# Builtins a script may see. Anything that reaches the filesystem, the
# network, the import system, or the object graph is absent by construction.
SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "bytearray": bytearray,
    "bytes": bytes,
    "callable": callable,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "hash": hash,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "RuntimeError": RuntimeError,
    "ZeroDivisionError": ZeroDivisionError,
    "StopIteration": StopIteration,
    "StopAsyncIteration": StopAsyncIteration,
    "print": print,
    "__build_class__": __build_class__,
    "__import__": _guarded_import,
    "__name__": "adapter_script",
}


DEFAULT_PHASES = frozenset({"request", "response"})
KNOWN_PHASES = frozenset(
    {
        "auth",
        "request",
        "response",
        "poll_request",
        "poll_response",
        # Cascade-only: shapes a partial result when a degradable stage fails.
        # Without it, a degraded cascade would return the raw inter-stage
        # artefact, which is a handoff shape rather than a client response.
        "degraded",
    }
)

# Same alphabet the X-Stages header accepts: stage names end up in phase
# strings and span attributes.
STAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# A script declaring a long STAGES list turns one inbound request into that
# many upstream calls, so the declaration itself is bounded.
MAX_DECLARED_STAGES = 8


def _parse_stages(declared: Any) -> tuple[str, ...]:
    """Reads the module-level STAGES declaration (AC-24 / AC-25)."""
    if declared is None:
        return ()
    if isinstance(declared, str) or not isinstance(declared, (list, tuple)):
        raise SecurityError("STAGES must be a list of stage names")
    names = [str(s).strip() for s in declared]
    if not names:
        # An empty list is the same statement as not declaring one at all.
        return ()
    for name in names:
        if not STAGE_NAME_RE.match(name):
            raise SecurityError(
                f"Stage name {name!r} must match [A-Za-z0-9_-]+"
            )
    if len(set(names)) != len(names):
        raise SecurityError("STAGES contains duplicate stage names")
    if len(names) > MAX_DECLARED_STAGES:
        raise SecurityError(
            f"STAGES declares {len(names)} stages, over the "
            f"{MAX_DECLARED_STAGES} stage limit"
        )
    return tuple(names)


def _parse_fallback(declared: Any, stages: tuple[str, ...]) -> frozenset[str]:
    """Reads STAGE_FALLBACK: which stages may be dropped on failure."""
    if declared is None:
        return frozenset()
    if isinstance(declared, str) or not isinstance(
        declared, (list, tuple, set, frozenset)
    ):
        raise SecurityError("STAGE_FALLBACK must be a list of stage names")
    names = frozenset(str(s).strip() for s in declared)
    if not names:
        return frozenset()
    if not stages:
        raise SecurityError(
            "STAGE_FALLBACK requires STAGES to be declared as well"
        )
    unknown = sorted(names - set(stages))
    if unknown:
        raise SecurityError(
            f"STAGE_FALLBACK names stages absent from STAGES: {unknown}"
        )
    if stages[0] in names:
        # Degrading the first stage would mean returning nothing at all: there
        # is no prior artefact to fall back to.
        raise SecurityError(
            f"The first stage {stages[0]!r} cannot be in STAGE_FALLBACK"
        )
    return names


PHASE_DEGRADED = "degraded"


@dataclass(frozen=True)
class CompiledScript:
    sha256: str
    origin: str
    transform: Callable[..., Any]
    phases: frozenset[str] = DEFAULT_PHASES
    # Empty means single-stage: the phase names carry no suffix and the engine
    # takes the original one-call path.
    stages: tuple[str, ...] = ()
    fallback: frozenset[str] = frozenset()

    def handles(self, phase: str) -> bool:
        return phase in self.phases

    @property
    def staged(self) -> bool:
        return bool(self.stages)

    def may_degrade(self, stage: str) -> bool:
        """Whether losing this stage is survivable (AC-28)."""
        return stage in self.fallback


class ScriptCache:
    """Thread-safe LRU of compiled scripts keyed by source digest."""

    def __init__(self, max_size: int = 256) -> None:
        self._max_size = max(1, max_size)
        self._entries: OrderedDict[str, CompiledScript] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sha256: str) -> CompiledScript | None:
        with self._lock:
            entry = self._entries.get(sha256)
            if entry is not None:
                self._entries.move_to_end(sha256)
            return entry

    def put(self, script: CompiledScript) -> None:
        with self._lock:
            self._entries[script.sha256] = script
            self._entries.move_to_end(script.sha256)
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def load(self, source: ScriptSource) -> CompiledScript:
        """Returns a compiled script, scanning and compiling on cache miss."""
        cached = self.get(source.sha256)
        if cached is not None:
            return cached

        filename = f"<script:{source.origin}:{source.sha256[:12]}>"
        scan_source(source.text, filename=filename)
        code = compile(source.text, filename, "exec")

        namespace: dict[str, Any] = {"__builtins__": SAFE_BUILTINS}
        try:
            exec(code, namespace)  # noqa: S102 - sandboxed by scan + SAFE_BUILTINS
        except Exception as exc:
            raise SecurityError(
                f"Script failed at import time: {type(exc).__name__}"
            ) from exc

        fn = namespace.get(TRANSFORM_NAME)
        if fn is None or not callable(fn):
            raise SecurityError(
                f"Script must define an async function named {TRANSFORM_NAME}(ctx, payload, phase)"
            )

        # Optional module-level opt-in for phases beyond request/response:
        #   PHASES = ["auth", "request", "response"]
        declared = namespace.get("PHASES")
        if declared is None:
            phases = DEFAULT_PHASES
        else:
            if isinstance(declared, str) or not isinstance(
                declared, (list, tuple, set, frozenset)
            ):
                raise SecurityError("PHASES must be a list of phase names")
            phases = frozenset(str(p).strip().lower() for p in declared)
            unknown = phases - KNOWN_PHASES
            if unknown:
                raise SecurityError(
                    f"PHASES contains unknown phases: {sorted(unknown)}"
                )
            missing = DEFAULT_PHASES - phases
            if missing:
                raise SecurityError(
                    f"PHASES must include {sorted(DEFAULT_PHASES)}; missing {sorted(missing)}"
                )

        stages = _parse_stages(namespace.get("STAGES"))
        fallback = _parse_fallback(namespace.get("STAGE_FALLBACK"), stages)

        compiled = CompiledScript(
            sha256=source.sha256,
            origin=source.origin,
            transform=fn,
            phases=phases,
            stages=stages,
            fallback=fallback,
        )
        self.put(compiled)
        return compiled
