"""Channel-declared model mapping: ``X-Channel-Options.model_map``.

The control plane names the model the caller asked for; the upstream usually
has a different name for the same thing. This module is the whole of that
translation -- a table per channel, resolved once per request by
``adapter.api.pipeline.adapt`` and written into the canonical body before any
script sees it.

    X-Channel-Options: {"model_map": {"gpt-image-1": "doubao-seedream-5-0-260128"}}
    X-Channel-Options: {"model_map": {"*": "doubao-seedream-5-0-260128"}}

Three rules decide the shape of everything here.

**No table means no mapping at all.** A channel that declares nothing behaves
byte-for-byte as it did before this existed: no default rewrite, no synthesised
identity table. That is what makes the feature free for every existing channel,
and it is why the option lives in the channel headers rather than in code.

**Exact match beats the wildcard, and there is at most one wildcard.**
``{"gpt-image-2": "A", "*": "B"}`` sends ``gpt-image-2`` to ``A`` and
everything else to ``B``. ``*`` is the catch-all for the channel that fronts
exactly one upstream model -- which is the case the short form exists for -- and
it also catches a request that carries no model at all, since "this channel
speaks one model" is a statement about the channel, not about the request.
The table is therefore an exact map plus at most one catch-all, never a pattern
language: a partial glob is refused (below), and so is anything that would make
the catch-all ambiguous -- a repeated key on the wire, which the header decoder
rejects (``adapter/channel.py``), and two spellings of one key that differ only
by whitespace, which ``parse`` rejects here.

**No match means passthrough.** The table translates; it does not filter. A
request whose model is not listed keeps the name it came with, which is the
reading New API gives its own model mapping. The consequence is worth stating
plainly: a channel that declares a table without ``*`` can still forward an
unlisted name upstream. That is the caller's own routing label reaching the
vendor -- the alternative (refusing unlisted models) would turn "add a mapping"
into "narrow the set of models this channel accepts", a much larger change to
make under the same header.

Two shapes are refused rather than ignored, because an operator who declared
something the adapter cannot honour must not be left believing it is in effect:
a ``model_map`` that is not an object of non-empty strings, and any key that
contains ``*`` other than the bare wildcard. The second one is the interesting
case -- ``{"gemini-*": "x"}`` looks like a pattern and would silently never
match, which is exactly the failure mode (a declared knob that quietly does
nothing) this module exists to avoid. Globs can be added the day someone needs
them; guessing at them now would buy a silent no-op.

Matching is case-sensitive: model ids are.
"""

from __future__ import annotations

from adapter.errors import ChannelConfigError

#: The catch-all key. Spelled once and read everywhere else from here.
WILDCARD = "*"

#: Named in every refusal, so the message points the operator at one header.
HEADER = "X-Channel-Options"
PARAM = "X-Channel-Options.model_map"


def _refuse(detail: str) -> ChannelConfigError:
    """One channel configuration error, phrased the way the other headers are.

    The code and the param matter more than the text: ``channel_config_error``
    with ``param`` on the header is what sends an operator to their channel
    configuration instead of to the client's request body.
    """
    return ChannelConfigError(f"{PARAM} {detail}", HEADER)


def parse(raw: object) -> dict[str, str]:
    """Validates one ``model_map`` value and returns the table to match against.

    An absent option (``raw is None``) is not a mistake: it is the default, and
    it yields the empty table -- the "no mapping" case above. Whitespace around
    a key or a value is stripped, since both are typed by hand into a header;
    a key or value that is *only* whitespace is refused, because it is a typo
    rather than a deliberate entry. Stripping is also what makes two keys collide
    (``"*"`` and ``" * "``), so a repeated key after trimming is refused instead
    of quietly overwriting: one catch-all is the rule, and a table that holds two
    only because of whitespace is the same ambiguity a repeated JSON key is.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _refuse("must be a JSON object of model names")
    table: dict[str, str] = {}
    for key, value in raw.items():
        # A JSON object key is always a string, but this function is also
        # reachable from a script-side table or a test fixture, and a non-string
        # key would otherwise be stored and then never match anything.
        name = key.strip() if isinstance(key, str) else ""
        if not name:
            raise _refuse("keys must be non-empty strings")
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
        if not isinstance(value, str) or not value.strip():
            raise _refuse(f"value for {name!r} must be a non-empty string")
        table[name] = value.strip()
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
