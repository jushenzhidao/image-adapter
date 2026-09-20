"""Channel-declared model mapping: the ``X-Model-Map`` header.

The control plane names the model the caller asked for; the upstream usually
has a different name for the same thing. This module is the whole of that
translation -- a table per channel, resolved once per request by
``adapter.api.pipeline.adapt`` and written into the canonical body before any
script sees it.

    X-Model-Map: gpt-image-1=doubao-seedream-5-0-260128
    X-Model-Map: *=doubao-seedream-5-0-260128
    X-Model-Map: gpt-image-1=doubao-x, gpt-image-2=doubao-y, *=doubao-z

Four rules decide the shape of everything here.

**No table means no mapping at all.** A channel that declares nothing behaves
byte-for-byte as it did before this existed: no default rewrite, no synthesised
identity table. That is what makes the feature free for every existing channel,
and it is why the option lives in a channel header rather than in code.

**Exact match beats the wildcard, and there is at most one wildcard.**
``gpt-image-2=a,*=b`` sends ``gpt-image-2`` to ``a`` and everything else to
``b``. ``*`` is the catch-all for the channel that fronts exactly one upstream
model -- which is the case the short form exists for -- and it also catches a
request that carries no model at all, since "this channel speaks one model" is
a statement about the channel, not about the request. The table is therefore an
exact map plus at most one catch-all, never a pattern language: a partial glob
is refused (below), and so is a repeated key -- including two spellings of one
key that differ only by whitespace, which trimming makes collide.

**No match means passthrough.** The table translates; it does not filter. A
request whose model is not listed keeps the name it came with, which is the
reading New API gives its own model mapping. The consequence is worth stating
plainly: a channel that declares a table without ``*`` can still forward an
unlisted name upstream. That is the caller's own routing label reaching the
vendor -- the alternative (refusing unlisted models) would turn "add a mapping"
into "narrow the set of models this channel accepts", a much larger change to
make under the same header.

**Flat ``key=model`` pairs, not a JSON object.** An HTTP field value is a
string, so a table has to be serialised either way; the flat spelling is the
one that survives being embedded inside *another* JSON document without
escaping -- a channel configuration submitted as a JSON payload, a ConfigMap,
an IaC template. It is also the shape ``X-Stage-Urls`` and ``X-Async`` already
use, and the duplicate-key check falls out of building the dict rather than
needing a decoder hook. The cost, stated plainly: ``,`` and ``=`` cannot appear
inside a key or a value. Model ids are ``[A-Za-z0-9._-]``, and ``X-Stage-Urls``
carries the same restriction for the same reason.

Two shapes are refused rather than ignored, because an operator who declared
something the adapter cannot honour must not be left believing it is in effect:
a pair with an empty key or an empty model, and any key that contains ``*``
other than the bare wildcard. The second one is the interesting case --
``gemini-*=x`` looks like a pattern and would silently never match, which is
exactly the failure mode (a declared knob that quietly does nothing) this
module exists to avoid. Globs can be added the day someone needs them; guessing
at them now would buy a silent no-op.

Matching is case-sensitive: model ids are.
"""

from __future__ import annotations

from adapter.errors import ChannelConfigError

#: The catch-all key. Spelled once and read everywhere else from here.
WILDCARD = "*"

#: Named in every refusal, so the message points the operator at one header.
HEADER = "X-Model-Map"

#: The key this feature used to live under, inside ``X-Channel-Options``. No
#: longer read; ``channel.py`` refuses it by name, so a channel that still
#: carries it fails loudly instead of sending an un-rewritten model upstream.
LEGACY_KEY = "model_map"

#: The spelling every refusal repeats, so an operator who has never seen the
#: format learns it from the error rather than from the docs.
_EXAMPLE = "key=model pairs, e.g. gpt-image-2=doubao-seedream-5-0-260128"


def _refuse(detail: str) -> ChannelConfigError:
    """One channel configuration error, phrased the way the other headers are.

    The code and the param matter more than the text: ``channel_config_error``
    with ``param`` on the header is what sends an operator to their channel
    configuration instead of to the client's request body.
    """
    return ChannelConfigError(f"{HEADER} {detail}", HEADER)


def parse(raw: object) -> dict[str, str]:
    """Validates one ``X-Model-Map`` value and returns the table to match against.

    An absent header (``raw is None``) is not a mistake: it is the default, and
    it yields the empty table -- the "no mapping" case above. The same holds for
    a present-but-blank value, which is what a control plane sends for an option
    it has cleared.

    Whitespace around a key or a model is stripped, since both are typed by hand
    into a header; a key or model that is *only* whitespace is refused, because
    it is a typo rather than a deliberate entry. Stripping is also what makes two
    keys collide (``*`` and `` * ``), so a repeated key after trimming is refused
    instead of quietly overwriting: one catch-all is the rule, and a table that
    holds two only because of whitespace is the same ambiguity a repeated entry
    is.

    The value is split on ``,`` first and on the *first* ``=`` of each pair
    second, so a stray ``=`` inside a model name is carried along rather than
    truncating the name. A ``,`` cannot be told from the separator, which is the
    documented limit of the format.
    """
    if raw is None:
        return {}
    # A non-string can only arrive from a fixture or a script-side table: the
    # header is a string by the time this is called. Refusing beats storing a
    # key that could never be matched.
    if not isinstance(raw, str):
        raise _refuse(f"must use {_EXAMPLE}")
    if not raw.strip():
        return {}

    table: dict[str, str] = {}
    for token in raw.split(","):
        token = token.strip()
        if not token:
            # A trailing comma, or padding between pairs: `a=b, c=d` is how a
            # human writes a list, and refusing it would teach nothing.
            continue
        name, sep, value = token.partition("=")
        name = name.strip()
        value = value.strip()
        if not sep or not name or not value:
            raise _refuse(f"must use {_EXAMPLE}")
        if name in table:
            raise _refuse(
                f"repeats the key {name!r}; the table is exact entries plus at "
                f"most one {WILDCARD!r} catch-all"
            )
        if name != WILDCARD and WILDCARD in name:
            raise _refuse(
                f"supports only the exact key {WILDCARD!r} as a catch-all; "
                f"{name!r} would never match"
            )
        table[name] = value
    return table


def resolve(model: object, table: dict[str, str]) -> str | None:
    """The upstream model this request must be sent as, or None to leave it.

    ``None`` is the "nothing to do" answer, and it is the only one that lets
    the caller keep the body untouched: an empty table, a model that is not
    listed, and a ``model`` that is not even a string all mean "send what the
    client sent" (the last one may be a malformed body, but choosing a model
    for it is not this function's call -- the body validator owns that).

    A falsy or non-string ``model`` still matches ``*``: a channel fronting one
    upstream model should answer the same way to a client that named a model
    and to one that named none.
    """
    if not table:
        return None
    name = model.strip() if isinstance(model, str) else ""
    if name and name in table:
        return table[name]
    return table.get(WILDCARD)
