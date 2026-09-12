"""Logfire (OpenTelemetry) initialisation.

The choices below differ from the obvious implementation.

**Only api keys are redacted.** The scrubbing pass stays on -- it is the only
mechanism that can catch a key sitting *inside* a value, such as the
``?api_key=...`` an ``X-Auth-Emit: query:...`` channel puts in the upstream URL
-- but its callback hands back every match that is **not** an api-key name. So a
prompt, a presigned link, a ``password``-flavoured word: all arrive verbatim, and
the redaction is aimed at one class of thing rather than at anything that looked
suspicious. The name list is a module constant, not a setting: it is not a
per-deployment decision, and a knob whose only two positions are "protect keys"
and "do not" does not need an environment variable.

**Request headers are never captured, and turning it on is refused.** The
channel contract carries ``X-Adapter-Key`` (the data-plane credential),
``Authorization`` (the upstream vendor's credential), ``X-Auth-Emit`` (where the
credential goes -- and it may name *any* header, e.g. ``x-goog-api-key``) and
``X-Script`` (executable Python source). ``init_logfire`` raises if
``capture_headers`` is on: a fixed name list cannot cover a header name the
control plane picks per channel, and a service that starts is a service nobody
re-reads the startup log of.

**Shutdown flushes the queue.** The batch exporter parks spans on a *daemon*
thread and registers no ``atexit`` hook
(``opentelemetry.sdk._shared_internal.BatchProcessor``), so an exit that does not
flush drops whatever is still queued -- up to one batch interval worth.
``flush_spans`` closes that window, and closing it is what makes the longer
interval below safe.

**The batch interval is set here, not in the environment.** logfire reads
``OTEL_BSP_SCHEDULE_DELAY`` when it builds its exporter and exposes no argument
for it; its own default is 500 ms where OTel's is 5000. Five seconds is the right
value for this service -- requests take tens of seconds, so a trace appearing
five seconds later costs nothing, while the export request count drops by roughly
10x -- and hardcoding it means the tuning cannot be lost by a deployment that
omits one line.

**/health is excluded.** Every orchestrator probes it on a timer, and those
spans would outnumber real traffic by orders of magnitude and bury it.

**The reported version has one source.** It used to be a literal in two files
that had already drifted apart ("2.0.0" in main.py vs "1.0.0" here, against a
1.0.0 package). It now resolves settings -> installed metadata ->
pyproject.toml, so the dashboard cannot disagree with the artifact.
"""

from __future__ import annotations

import logging
import os
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING

import logfire
from logfire import ConsoleOptions, SamplingOptions, ScrubbingOptions

from adapter.settings import BASE_DIR

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, WebSocket

    from adapter.settings import Settings

logger = logging.getLogger(__name__)

# Set before anything can build the exporter: logfire's `DynamicBatchSpanProcessor`
# reads this at construction (inside `logfire.configure`), and a deployment that
# omitted the line would silently fall back to logfire's 500 ms. `setdefault`
# rather than an assignment so an explicit value still wins if one is ever needed.
os.environ.setdefault("OTEL_BSP_SCHEDULE_DELAY", "5000")

#: Credential *names* this adapter knows about, on top of the ones logfire already
#: ships (`api[._ -]?key`, and the `auth` that covers `authorization`).
#:
#: Bare names, with no `[:=]value` tail, and that shape is load-bearing: a pattern
#: that runs to the end of the value matches the *whole* value, and logfire treats a
#: whole-string match as safe ("the value is literally 'password'") without ever
#: consulting the callback. These patterns used to end in `\s*\S+`, so a value that
#: was exactly one header line -- the case they were written for -- stayed in clear
#: text, and being the leftmost match it also shadowed the default pattern that
#: would have caught the credential inside it. The tail is now the callback's job
#: instead (see `_is_assignment_after`).
_SCRUB_PATTERNS: tuple[str, ...] = (
    r"x-adapter-key",
    r"x-auth-emit",
    r"x-script(?:-64)?",
)

#: Normalised names that mean "a key lives here". Normalised because the scrubber
#: matches case-insensitively across `_`, `-`, `.` and spaces: `api_key`,
#: `API-KEY` and `api key` are one entry.
_KEY_NAMES = frozenset(
    {
        "apikey",  # logfire's api[._ -]?key: covers x-goog-api-key too
        "auth",  # the `auth` inside `authorization` and `x-auth-emit`
        "authorization",
        "xadapterkey",
        "xauthemit",
        "xscript",
        "xscript64",
        "logfiretoken",  # the token that authorises writes to the project
    }
)

#: `name: value` / `name=value` / `name: "value"` -- the rest of the name, then a
#: separator. This is what separates a credential line from prose that merely
#: mentions the word, and it deliberately does not require a trailing value: a
#: match has to stop short of the end of the string to be redactable at all.
_ASSIGNMENT = re.compile(r"""[A-Za-z0-9._-]*\s*["']?\s*[:=]""")

#: How long the shutdown flush may take.
#:
#: Bounded on purpose: gunicorn sends SIGKILL once ``GUNICORN_GRACEFUL_TIMEOUT``
#: (120 s here) runs out, and a flush that waits on an unreachable Logfire must
#: give up well inside that rather than turn a slow collector into a killed
#: worker.
_SHUTDOWN_FLUSH_MS = 5_000


def _is_key_name(text: str) -> bool:
    normalised = re.sub(r"[^a-z0-9]", "", text.lower())
    return normalised in _KEY_NAMES or normalised.startswith("pylf")


def _attribute_name(path: tuple) -> str:
    """The attribute name in a scrub path: ``('attributes', 'result_urls', 0)``."""
    for element in reversed(path):
        if isinstance(element, str) and element != "attributes":
            return element
    return ""


def _is_assignment_after(value: object, match: re.Match[str]) -> bool:
    """Whether the matched name continues into ``: value`` or ``= value``.

    ``match.end()`` is where the *name* stopped, which for ``authorization`` is
    after ``auth`` -- logfire's own pattern stops there so that it does not match
    the ``auth`` inside words like "author". ``_ASSIGNMENT`` therefore re-reads
    whatever is left of the name before demanding the separator.
    """
    if not isinstance(value, str):
        return False
    return _ASSIGNMENT.match(value, match.end()) is not None


def _scrubbing_callback(match: logfire.ScrubMatch) -> str | None:
    """Redact api keys; hand everything else back unchanged.

    Two properties of the callback contract shape this:

    * The scrubber only calls it on a **match**, and returning anything other
      than ``None`` **cancels the redaction for that match** (logfire's
      ``SpanScrubber._redact`` returns immediately). ``return match.value`` is
      therefore this adapter's "not a credential, keep it" -- and a branch that
      forgets to fall through to it exports the value instead of hiding it.
    * A match against an attribute *name* and a match inside a *value* arrive in
      the same shape, so they are told apart by the name (``matched in name``)
      and, for values, by requiring the assignment shape. Without that second
      test any prompt mentioning "api key" would be discarded whole, and the
      prompt is the one attribute a refused request is read for.
    """
    matched = match.pattern_match.group(0)
    if not _is_key_name(matched):
        return match.value
    if matched.lower() in _attribute_name(match.path).lower():
        return None
    if _is_assignment_after(match.value, match.pattern_match):
        return None
    return match.value


def flush_spans(timeout_millis: int = _SHUTDOWN_FLUSH_MS) -> bool:
    """Drains queued spans before the process goes away.

    See the module docstring: the batch exporter is a daemon thread with no
    ``atexit`` hook, so without this the last batch interval of spans is dropped
    on every restart -- which is exactly the window a longer interval widens.

    Returns whether the flush finished inside the deadline. A ``False`` is
    reported rather than raised: shutdown is already under way, and the requests
    this worker is still finishing matter more than the traces already owed.
    """
    flushed = logfire.force_flush(timeout_millis=timeout_millis)
    if not flushed:
        logger.warning(
            "Logfire flush incomplete after %d ms; the queued spans are dropped",
            timeout_millis,
        )
    return flushed


def resolve_service_version(settings: Settings) -> str:
    """Single source of truth for the version reported to Logfire."""
    if settings.logfire_service_version:
        return settings.logfire_service_version
    try:
        return _pkg_version("image-adapter")
    except PackageNotFoundError:
        pass
    # The image copies the source tree without installing the package, so
    # distribution metadata is absent there; pyproject.toml is the next best
    # authority, and "unknown" is honest when neither is readable.
    try:
        import tomllib

        with open(BASE_DIR / "pyproject.toml", "rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:  # noqa: BLE001 - any failure means "cannot determine"
        return "unknown"


def _request_attributes(request: Request | WebSocket, attributes: dict) -> dict:
    """Attach the adapter's request id so a trace can be found by it (BR-010)."""
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    if request_id:
        attributes["request_id"] = request_id
    return attributes


def init_logfire(app: FastAPI, settings: Settings) -> None:
    """Configure Logfire. Without a token it runs locally and exports nothing."""
    # Refused rather than warned about: a fixed name list cannot cover a header
    # name the control plane picks per channel (see the module docstring), and a
    # service that starts is a service nobody re-reads the startup log of.
    if settings.logfire_capture_headers:
        raise ValueError(
            "LOGFIRE_CAPTURE_HEADERS=true is refused: X-Adapter-Key, "
            "Authorization and X-Script would leave in clear text, and "
            "X-Auth-Emit names the credential header per channel, so no fixed "
            "redaction list can cover it. Write a reviewed redaction set in "
            "adapter/logfire_setup.py first."
        )

    sample_rate = min(max(settings.logfire_sample_rate, 0.0), 1.0)
    service_version = resolve_service_version(settings)

    logfire.configure(
        token=settings.logfire_token or None,
        service_name=settings.logfire_service_name,
        service_version=service_version,
        environment=settings.environment,
        send_to_logfire="if-token-present",
        # Human-readable spans on stderr are useful locally and noise in
        # production; the remote export behaves identically either way.
        console=ConsoleOptions() if settings.logfire_console else False,
        sampling=SamplingOptions(head=sample_rate),
        scrubbing=ScrubbingOptions(
            extra_patterns=list(_SCRUB_PATTERNS), callback=_scrubbing_callback
        ),
    )

    # ``logging`` is a separate pipeline from the tracing above, and until this line was
    # added nothing connected them: the app logged to stdout and to Logfire's own spans,
    # and every ``logger.warning`` in the codebase -- including the storage layer's report
    # that an address went dark -- stopped at the container boundary.
    #
    # WARNING and above, not NOTSET. This app logs progress at INFO, and shipping every
    # line of it to a hosted backend is a different (and much larger) decision than
    # shipping the lines that say something went wrong. INFO keeps going to stdout for the
    # platform's own collector, so nothing is lost, only unshipped.
    #
    # The fallback is a NullHandler because ``main.py`` already installs a stderr handler
    # through ``logging.basicConfig``: the fallback only runs when logfire instrumentation
    # is suppressed, and leaving logfire's default in place would print those records
    # twice.
    #
    # Attached once. ``init_logfire`` runs per app and the test suite builds several, and
    # a second handler would ship every record twice rather than failing.
    root = logging.getLogger()
    if not any(isinstance(h, logfire.LogfireLoggingHandler) for h in root.handlers):
        root.addHandler(
            logfire.LogfireLoggingHandler(
                level=logging.WARNING, fallback=logging.NullHandler()
            )
        )

    if settings.logfire_token:
        logger.info(
            "Logfire enabled: service=%s version=%s sampling=%.2f excluded=%s "
            "scrubbing=api-keys-only",
            settings.logfire_service_name,
            service_version,
            sample_rate,
            settings.logfire_excluded_urls or "(none)",
        )
    else:
        logger.warning("Logfire running in local-only mode (LOGFIRE_TOKEN not set)")
    logfire.instrument_fastapi(
        app,
        # Refused above rather than merely documented: see the module docstring.
        capture_headers=False,
        excluded_urls=settings.logfire_excluded_urls or None,
        request_attributes_mapper=_request_attributes,
    )
