"""Channel spec: the control plane's adaptation directive, carried in headers.

One channel == one upstream endpoint. New API owns model/billing/routing and
declares only how to talk to that endpoint:

  X-Upstream-Url   https://api.vendor-x.com/v2/text2img   (required)
  X-Upstream-Proxy http://127.0.0.1:3128    outbound proxy, this channel only
  X-Script         inline source, literal \\n as separator (one of three)
  X-Script-64      base64 of the source                   (one of three)
  X-Script-Ref     vendor_y/mj@v1.3 or https://.../mj.py  (one of three)
  Authorization    upstream credential, passed through
  X-Adapter-Key    admission key for this data plane
  X-Upstream-Method  default POST
  X-Auth-Emit      header:X-API-Key:Bearer
  X-Async          poll=2,timeout=300
  X-Script-Sha256  integrity pin
  X-Model-Map      gpt-image-2=doubao-x,*=doubao-y   key=model pairs, this
                   channel; the one directive the adapter itself acts on
  X-Channel-Options  JSON object handed to the script as ctx.options -- decoded
                   here, but nothing in it is the adapter's business
  X-Stages         generate,upscale        overrides the script's STAGES
  X-Stage-Urls     generate=https://a/t2i,upscale=https://b/sr
  X-Stage-Timeout  total=300               cascade budget, capped by settings
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from adapter.errors import ChannelConfigError
from adapter.modelmap import LEGACY_KEY as LEGACY_MODEL_MAP_KEY
from adapter.modelmap import parse as parse_model_map
from adapter.proxyplan import ProxyPlan, parse_hosts
from adapter.settings import Settings
from adapter.stage_spec import StageSpec
from adapter.urlguard import check_proxy_url, check_url

H_URL = "x-upstream-url"
H_METHOD = "x-upstream-method"
H_PROXY = "x-upstream-proxy"
H_SCRIPT = "x-script"
H_SCRIPT_64 = "x-script-64"
H_SCRIPT_REF = "x-script-ref"
H_SCRIPT_SHA = "x-script-sha256"
H_AUTH_EMIT = "x-auth-emit"
H_ASYNC = "x-async"
H_MODEL_MAP = "x-model-map"
H_OPTIONS = "x-channel-options"
H_ADAPTER_KEY = "x-adapter-key"
#: Not an `X-` header: it carries the *vendor* credential, not a directive.
#: It gets a constant like the rest because it is part of the same contract
#: (it is declared to FastAPI and allowed through CORS), and a literal at the
#: one call site was the only thing keeping the three lists from being
#: checkable against each other -- see
#: tests/unit/test_channel_headers_contract.py.
H_AUTHORIZATION = "authorization"

VALID_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
AUTH_TARGETS = frozenset({"header", "query", "body", "none"})


@dataclass(frozen=True)
class AuthEmit:
    """Where the upstream credential goes. Default: Authorization: Bearer <key>."""

    target: str = "header"
    name: str = "Authorization"
    prefix: str = "Bearer"

    @classmethod
    def parse(cls, raw: str) -> AuthEmit:
        parts = [p.strip() for p in raw.split(":")]
        target = parts[0].lower() if parts else "header"
        if target not in AUTH_TARGETS:
            raise ChannelConfigError(
                f"X-Auth-Emit target must be one of {sorted(AUTH_TARGETS)}", "X-Auth-Emit"
            )
        if target == "none":
            return cls(target="none", name="", prefix="")
        if len(parts) < 2 or not parts[1]:
            raise ChannelConfigError(
                "X-Auth-Emit requires a field name, e.g. header:X-API-Key", "X-Auth-Emit"
            )
        prefix = parts[2] if len(parts) > 2 else ""
        return cls(target=target, name=parts[1], prefix=prefix)


@dataclass(frozen=True)
class AsyncSpec:
    """Job-polling policy for async upstreams."""

    enabled: bool = False
    poll_interval: float = 2.0
    timeout: float = 300.0

    @classmethod
    def parse(cls, raw: str, settings: Settings) -> AsyncSpec:
        interval = settings.poll_interval_default
        timeout = settings.poll_timeout_default
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" not in token:
                raise ChannelConfigError(
                    "X-Async must use key=value pairs, e.g. poll=2,timeout=300", "X-Async"
                )
            key, _, value = token.partition("=")
            key = key.strip().lower()
            try:
                parsed = float(value.strip())
            except ValueError:
                raise ChannelConfigError(
                    f"X-Async value for '{key}' must be numeric", "X-Async"
                ) from None
            if parsed <= 0:
                raise ChannelConfigError(
                    f"X-Async value for '{key}' must be positive", "X-Async"
                )
            if key in {"poll", "interval"}:
                interval = parsed
            elif key == "timeout":
                timeout = parsed
            else:
                raise ChannelConfigError(f"Unknown X-Async key '{key}'", "X-Async")
        return cls(enabled=True, poll_interval=interval, timeout=timeout)


@dataclass(frozen=True)
class ChannelSpec:
    """Fully parsed, validated channel directive for one request."""

    upstream_url: str
    method: str = "POST"
    inline_script: str | None = None
    script_b64: str | None = None
    script_ref: str | None = None
    script_sha256: str | None = None
    upstream_key: str = ""
    # Its own header rather than a key in `options`: how the adapter reaches
    # the vendor is transport, not vendor dialect, and it has to be validated
    # by the framework (a bad value leaks the credential to a third party).
    # `options` is the script's bag, handed over unread.
    #
    # A plan rather than a bare URL, because two more decisions travel with it:
    # which hosts it covers, and whether each client request gets its own exit
    # (adapter/proxyplan.py). The pipeline attaches the request's session id by
    # replacing this field, so every layer downstream reads one value.
    proxy: ProxyPlan = field(default_factory=ProxyPlan)
    auth: AuthEmit = field(default_factory=AuthEmit)
    async_spec: AsyncSpec = field(default_factory=AsyncSpec)
    options: dict = field(default_factory=dict)
    model_map: dict[str, str] = field(default_factory=dict)
    stages: StageSpec = field(default_factory=lambda: StageSpec())

    @property
    def stage_urls(self) -> dict[str, str]:
        return self.stages.urls


#: The four escapes X-Script defines. Anything else following a backslash keeps
#: both characters, so a sequence the format does not define survives into the
#: source unchanged rather than being silently eaten.
_UNESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(.)")


def _unescape_inline(raw: str) -> str:
    r"""Turns a single-line header value back into Python source.

    HTTP forbids bare newlines in header values, so X-Script carries the two
    characters backslash-n where a line break belongs. Tabs and carriage
    returns get the same treatment; a literal backslash is written \\.

    One regex pass instead of a character loop: the loop measured 0.345 ms on
    an 8 KiB script against 0.026 ms here, and this runs on the event loop
    before any handler work starts. The dot does not match a newline, so a
    trailing lone backslash is left as-is -- which is what the loop did too.
    """

    def replace(match: re.Match[str]) -> str:
        char = match.group(1)
        return _UNESCAPES.get(char, "\\" + char)

    return _ESCAPE_RE.sub(replace, raw)


def _object_without_repeats(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object that refuses a repeated key instead of keeping the last.

    Channel options are hand-written JSON, and a repeated key has no single
    reading: ``{"*": "a", "*": "b"}`` breaks the one-catch-all rule, while
    ``{"model": "a", "model": "b"}`` is the same mistake one level up. Every
    JSON parser keeps the last one and says nothing -- orjson, which decodes the
    request bodies, cannot even be asked not to -- so the check has to happen
    here, on the one header that is typed by hand. Nested objects (the mapping
    table included) go through the same hook, so the repeat is caught wherever it
    is, and the operator is told which key it was.

    Stdlib ``json`` rather than ``adapter.jsoncodec``: ``object_pairs_hook`` is not
    part of orjson's API, and this header is a few hundred bytes where orjson's
    speed is irrelevant.
    """
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise ChannelConfigError(
                f"X-Channel-Options repeats the key {key!r}", "X-Channel-Options"
            )
        seen[key] = value
    return seen


def _first_present(headers, *names: str) -> tuple[str | None, str | None]:
    """Returns (header_name, value) for the first name that carries a value."""
    for name in names:
        value = headers.get(name)
        if value is not None and value.strip():
            return name, value
    return None, None


@lru_cache(maxsize=8)
def parse_default_options(raw: str) -> dict:
    """Deployment-wide options merged *under* every channel's own.

    The value is process-fixed, so the cache holds one entry in practice; it
    exists so the per-request path never re-parses a string that cannot change.
    Refuses anything but a JSON object, by name: this is configuration, and the
    operator gets told which knob is wrong. `main.py` calls it once at startup,
    which turns a malformed value into a boot failure instead of a 400 the
    first request has to discover.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw, object_pairs_hook=_object_without_repeats)
    except ValueError:
        # json.JSONDecodeError and orjson.JSONDecodeError both subclass it.
        raise ChannelConfigError(
            "DEFAULT_CHANNEL_OPTIONS must be a JSON object",
            "DEFAULT_CHANNEL_OPTIONS",
        ) from None
    if not isinstance(parsed, dict):
        raise ChannelConfigError(
            "DEFAULT_CHANNEL_OPTIONS must be a JSON object",
            "DEFAULT_CHANNEL_OPTIONS",
        )
    return parsed


def parse_channel(headers, settings: Settings) -> ChannelSpec:
    """Builds a ChannelSpec from request headers. Raises ChannelConfigError."""
    raw_url = headers.get(H_URL, "")
    if not raw_url.strip():
        raise ChannelConfigError("X-Upstream-Url header is required", "X-Upstream-Url")
    upstream_url = check_url(raw_url, settings)

    method = (headers.get(H_METHOD) or "POST").strip().upper()
    if method not in VALID_METHODS:
        raise ChannelConfigError(
            f"X-Upstream-Method must be one of {sorted(VALID_METHODS)}",
            "X-Upstream-Method",
        )

    # Validated on the same rule as the upstream URL, and for a stronger
    # reason: a proxy that is unreachable, misspelled or unlisted has to fail
    # before the request goes out, not as an opaque 502 afterwards. Absent
    # header means "no proxy" and costs nothing.
    proxy_raw = (headers.get(H_PROXY) or "").strip()
    if not proxy_raw:
        proxy = ProxyPlan()
    else:
        url = check_proxy_url(proxy_raw, settings, "X-Upstream-Proxy")
        # The bypass list is the deployment's, not this channel's: what has to
        # stay direct (an object store upload) is the same answer for every
        # channel, and repeating it on each one is how the copies drift apart.
        proxy = ProxyPlan(
            url=url,
            bypass=parse_hosts(
                settings.upstream_proxy_bypass_hosts, "UPSTREAM_PROXY_BYPASS_HOSTS"
            ),
        )

    source_name, _ = _first_present(headers, H_SCRIPT, H_SCRIPT_64, H_SCRIPT_REF)
    if source_name is None:
        raise ChannelConfigError(
            "One of X-Script, X-Script-64 or X-Script-Ref is required", "X-Script"
        )
    provided = [
        n for n in (H_SCRIPT, H_SCRIPT_64, H_SCRIPT_REF) if (headers.get(n) or "").strip()
    ]
    if len(provided) > 1:
        raise ChannelConfigError(
            "Provide exactly one of X-Script, X-Script-64 or X-Script-Ref", "X-Script"
        )

    inline_raw = (headers.get(H_SCRIPT) or "").strip()
    inline_script = _unescape_inline(inline_raw) if inline_raw else None

    auth_raw = (headers.get(H_AUTH_EMIT) or "").strip()
    auth = AuthEmit.parse(auth_raw) if auth_raw else AuthEmit()

    async_raw = (headers.get(H_ASYNC) or "").strip()
    async_spec = AsyncSpec.parse(async_raw, settings) if async_raw else AsyncSpec()

    options_raw = (headers.get(H_OPTIONS) or "").strip()
    options: dict = {}
    if options_raw:
        try:
            parsed = json.loads(options_raw, object_pairs_hook=_object_without_repeats)
        except ValueError:
            # json.JSONDecodeError and orjson.JSONDecodeError both subclass it.
            raise ChannelConfigError(
                "X-Channel-Options must be a JSON object", "X-Channel-Options"
            ) from None
        if not isinstance(parsed, dict):
            raise ChannelConfigError(
                "X-Channel-Options must be a JSON object", "X-Channel-Options"
            )
        options = parsed

    # Deployment-wide defaults sit *under* the channel's own options -- see
    # `default_channel_options` in settings.py. The header wins on any shared
    # key, so a channel can always override one value; with the knob empty this
    # is a no-op and `options` keeps the exact dict today's code produced.
    defaults = parse_default_options(settings.default_channel_options)
    if defaults:
        options = {**defaults, **options}

    # The mapping has its own header rather than a key inside `options`, because
    # it is the one part of the channel directive the adapter itself acts on --
    # and `options` is the script's bag. Declaring it separately keeps that
    # boundary literal, and it is validated here instead of being left to a
    # script: a channel configuration mistake has to fail before the request
    # reaches an upstream, and every script -- the inline drafts included -- gets
    # the mapping for free. What a script does with the resolved model is still
    # its own business; the framework only supplies the answer
    # (adapter/modelmap.py).
    if LEGACY_MODEL_MAP_KEY in options:
        # Refused rather than ignored, and refused by name. A channel still
        # carrying the old key would send a model nobody rewrote, and reaching
        # the wrong upstream model without a word is the failure this feature
        # exists to prevent; ignoring it would leave the operator with a table
        # that reads as declared and does nothing.
        raise ChannelConfigError(
            "X-Channel-Options.model_map has moved to its own header: "
            "X-Model-Map, as key=model pairs, e.g. "
            "gpt-image-2=doubao-seedream-5-0-260128",
            "X-Channel-Options",
        )
    model_map = parse_model_map(headers.get(H_MODEL_MAP))

    authorization = (headers.get(H_AUTHORIZATION) or "").strip()
    upstream_key = authorization
    if authorization.lower().startswith("bearer "):
        upstream_key = authorization[len("bearer ") :].strip()

    sha = (headers.get(H_SCRIPT_SHA) or "").strip().lower() or None

    return ChannelSpec(
        upstream_url=upstream_url,
        method=method,
        inline_script=inline_script,
        script_b64=(headers.get(H_SCRIPT_64) or "").strip() or None,
        script_ref=(headers.get(H_SCRIPT_REF) or "").strip() or None,
        script_sha256=sha,
        upstream_key=upstream_key,
        proxy=proxy,
        auth=auth,
        async_spec=async_spec,
        options=options,
        model_map=model_map,
        stages=StageSpec.parse(headers, settings),
    )
