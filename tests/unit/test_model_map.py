"""``adapter/modelmap.py``: the channel's model table, parsed and matched.

The module is small on purpose -- the whole feature is one table lookup -- so
what is worth pinning is not the happy path but the decisions that are tempting
to make differently later:

  * an absent header is not an error, and it is not an identity table either;
  * ``*`` is a catch-all, not a pattern, so ``gemini-*=x`` is refused rather
    than accepted and silently never matched;
  * a request that matches nothing keeps the name it arrived with.

The last one is the zero-regression property the whole design rests on, so it
is asserted from both sides: an empty table, and a table that simply does not
list the model.

``parse_channel`` is exercised here too, because the header is validated there
rather than in a script: a channel-configuration mistake has to be a 400 before
the request reaches an upstream, and the only place that can happen is the
header parser.

The last group also pins the *retirement*: the table used to be a key inside
``X-Channel-Options``, and a channel that still carries it is refused by name
rather than ignored -- an ignored one would send a model nobody rewrote, which
is the failure this whole feature exists to prevent.
"""

from __future__ import annotations

import pytest

from adapter.channel import parse_channel
from adapter.errors import ChannelConfigError
from adapter.modelmap import HEADER, LEGACY_KEY, WILDCARD, parse, resolve
from adapter.settings import Settings

#: A minimal usable channel, lowercase because `parse_channel` reads raw header
#: names (the service hands it Starlette's case-insensitive mapping; a plain
#: dict in a unit test is not one).
BASE = {
    "x-upstream-url": "https://api.vendor.test/v1/images/generations",
    "x-script-ref": "openai/images@v1",
}


def _settings() -> Settings:
    # `_env_file=None`: a bare Settings() reads the production .env, which would
    # make this suite's outcome depend on the machine it runs on.
    return Settings(_env_file=None)


def _table(raw: str) -> dict:
    """One header value through the real channel parser.

    Used instead of ``parse`` where what is being pinned is the *header*, not
    the string handling: the header name is looked up by the parser, so a typo
    in it would leave the table empty and every assertion here vacuous.
    """
    return parse_channel({**BASE, HEADER.lower(): raw}, _settings()).model_map


# --- parse -------------------------------------------------------------------


def test_an_absent_header_is_the_empty_table():
    """Not declaring the header is the default, not a mistake."""
    assert parse(None) == {}


@pytest.mark.parametrize("raw", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_a_blank_header_is_the_empty_table(raw):
    """A control plane clearing an option sends an empty value, not no header."""
    assert parse(raw) == {}


def test_pairs_are_trimmed():
    """Both sides are typed by hand into a header, so padding is a typo."""
    assert parse(" gpt-image-2 = doubao-x ") == {"gpt-image-2": "doubao-x"}


def test_the_bare_wildcard_is_accepted():
    assert parse(f"{WILDCARD}=doubao-x") == {WILDCARD: "doubao-x"}


def test_several_pairs_are_comma_separated():
    assert parse("gpt-image-1=a,gpt-image-2=b,*=c") == {
        "gpt-image-1": "a",
        "gpt-image-2": "b",
        WILDCARD: "c",
    }


@pytest.mark.parametrize(
    "raw", ["a=b, c=d", "a=b ,c=d", "a=b,c=d,", "a=b,,c=d"], ids=["space", "lpad", "trailing", "double"]
)
def test_list_padding_is_tolerated(raw):
    """`a=b, c=d` is how a human writes a list; refusing it would teach nothing.

    An empty segment is a separator artefact, so it is skipped -- which is the
    same reading `X-Stage-Urls` gives the same shape.
    """
    assert parse(raw) == {"a": "b", "c": "d"}


def test_only_the_first_equals_sign_splits():
    """A stray `=` belongs to the model name, which is what keeping the rest does.

    Splitting on every `=` would silently truncate the name to `b`, i.e. a
    mapping the operator did not write -- the defect this file is about.
    """
    assert parse("a=b=c") == {"a": "b=c"}


@pytest.mark.parametrize(
    "raw",
    [["a=b"], 7, True, {"a": "b"}, None],
    ids=["list", "number", "boolean", "object", "none"],
)
def test_a_value_that_is_not_a_header_string_is_refused(raw):
    """Only a missing header is `{}`; anything that is not text is a fixture bug.

    `None` is called out separately because it is the one falsy input that *is*
    legitimate -- see `test_an_absent_header_is_the_empty_table`.
    """
    if raw is None:
        assert parse(raw) == {}
        return
    with pytest.raises(ChannelConfigError) as caught:
        parse(raw)
    assert caught.value.code == "channel_config_error"
    assert caught.value.param == HEADER


@pytest.mark.parametrize(
    "raw",
    ["gpt-image-2", "=doubao-x", "gpt-image-2=", "gpt-image-2=   ", "   =doubao-x"],
    ids=["no-equals", "blank-key", "blank-model", "padding-model", "padding-key"],
)
def test_a_pair_that_cannot_be_matched_is_refused(raw):
    """A blank key can never match and a blank model is not a name.

    Storing either would produce an entry that is present in the table and
    still never applies -- the silent no-op this module exists to avoid.
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse(raw)
    assert caught.value.code == "channel_config_error"
    assert caught.value.param == HEADER
    assert "key=model" in caught.value.message


@pytest.mark.parametrize("key", ["gemini-*", "gpt-*", "*gemini", "g*p-t"])
def test_a_partial_glob_is_refused(key):
    """``*`` is a catch-all, not a pattern -- so a pattern must not pass.

    This is the one refusal that is about honesty rather than typing: an
    operator who writes ``gemini-*=x`` believes they declared a rule, and a
    table that accepts it and never matches anything is worse than a 400 naming
    the key.
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse(f"{key}=doubao-x")
    assert key in caught.value.message
    assert WILDCARD in caught.value.message


@pytest.mark.parametrize(
    "raw",
    [
        "gpt-image-2=a, gpt-image-2=b",
        "*=a, *=b",
        f" {WILDCARD}=a,{WILDCARD}=b",
    ],
    ids=["exact-twice", "wildcard-twice", "wildcard-twice-after-trim"],
)
def test_a_repeated_key_is_refused(raw):
    """Two entries for one key have no single reading, and the second used to win.

    Trimming is what makes ``*`` and `` * `` collide, so the collision has to be
    caught after it, not before: one catch-all is the rule, and "which of the
    two" is not a question an operator should have to reason about. This check
    falls out of building the dict, which is why the flat form needs no decoder
    hook -- unlike the JSON bag next door, whose repeats are caught while the
    header is decoded (``test_a_repeated_json_key_is_still_refused``).
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse(raw)
    assert "repeats" in caught.value.message


# --- resolve -----------------------------------------------------------------


def test_an_empty_table_resolves_to_nothing():
    assert resolve("gpt-image-2", {}) is None


def test_an_exact_key_wins():
    assert resolve("gpt-image-2", {"gpt-image-2": "doubao-x"}) == "doubao-x"


def test_the_wildcard_catches_anything_else():
    assert resolve("whatever", {WILDCARD: "doubao-x"}) == "doubao-x"


def test_an_exact_key_beats_the_wildcard():
    table = {"gpt-image-2": "exact", WILDCARD: "catchall"}
    assert resolve("gpt-image-2", table) == "exact"
    assert resolve("gpt-image-1", table) == "catchall"


def test_an_unlisted_model_is_left_alone():
    """The table translates; it does not filter."""
    assert resolve("gpt-image-1", {"gpt-image-2": "doubao-x"}) is None


@pytest.mark.parametrize("model", [None, "", "   ", 7, ["x"], {"a": 1}])
def test_a_wildcard_answers_a_request_that_named_no_model(model):
    """A channel fronting one model should not be defeated by a silent client.

    The exception is a channel with no wildcard: there is nothing to answer
    with, so the body stays as it is and the script's own default applies.
    """
    assert resolve(model, {WILDCARD: "doubao-x"}) == "doubao-x"
    assert resolve(model, {"gpt-image-2": "doubao-x"}) is None


def test_the_model_is_matched_verbatim():
    """Model ids are case-sensitive, and a table must not pretend otherwise."""
    assert resolve("GPT-Image-2", {"gpt-image-2": "doubao-x"}) is None


# --- the channel header ------------------------------------------------------


def test_the_table_lands_on_the_channel_spec():
    spec = parse_channel(
        {
            **BASE,
            HEADER.lower(): f"{WILDCARD}=doubao-x",
            "x-channel-options": '{"watermark": false}',
        },
        _settings(),
    )
    assert spec.model_map == {WILDCARD: "doubao-x"}
    # The options bag still travels to the script untouched, and is still the
    # script's alone: the mapping is no longer a key inside it.
    assert spec.options["watermark"] is False


def test_a_channel_without_the_header_gets_an_empty_table():
    assert parse_channel(BASE, _settings()).model_map == {}


def test_an_unusable_table_fails_the_channel_rather_than_the_request():
    """The failure mode this pins: 400 before an upstream call, not a silent
    fallback to an unmapped model."""
    with pytest.raises(ChannelConfigError) as caught:
        parse_channel({**BASE, HEADER.lower(): "not-a-pair"}, _settings())
    assert caught.value.code == "channel_config_error"
    assert caught.value.status == 400
    assert caught.value.param == HEADER


def test_the_mapping_is_not_a_key_the_script_can_see():
    """`ctx.options` must not carry the table, in either direction.

    The boundary is the whole point of giving the mapping its own header: the
    adapter acts on it, the script is handed the rest unread. A script that
    found a `model_map` key here would be reading a table the framework never
    applied.
    """
    spec = parse_channel({**BASE, HEADER.lower(): f"{WILDCARD}=doubao-x"}, _settings())
    assert "model_map" not in spec.options


def test_the_old_option_key_is_refused_by_name():
    """The retirement, and the reason it is a refusal rather than a fallback.

    A channel still carrying the old key would have its table silently ignored
    and send whatever the caller named -- reaching the wrong upstream model
    without a word. The message names both headers so the fix is one edit.
    """
    raw = '{"model_map": "*=doubao-x", "watermark": false}'
    with pytest.raises(ChannelConfigError) as caught:
        parse_channel({**BASE, "x-channel-options": raw}, _settings())
    assert caught.value.code == "channel_config_error"
    assert caught.value.status == 400
    assert caught.value.param == "X-Channel-Options"
    assert LEGACY_KEY in caught.value.message
    assert HEADER in caught.value.message


def test_a_repeated_json_key_is_still_refused():
    """`X-Channel-Options` is still hand-written JSON, so its hook stays.

    Unrelated to the mapping -- pinned here because the mapping used to be the
    only reason anyone looked at this header closely, and the decoder hook must
    not be retired along with it.
    """
    raw = '{"model": "doubao-a", "model": "doubao-b"}'
    with pytest.raises(ChannelConfigError) as caught:
        parse_channel({**BASE, "x-channel-options": raw}, _settings())
    assert "repeats" in caught.value.message


def test_an_unrelated_option_is_still_the_scripts_business():
    """Nothing in the bag is validated -- this is the guard against that
    boundary creeping now that the one exception has moved out."""
    spec = parse_channel(
        {**BASE, "x-channel-options": '{"image_ref_mode": "data_uri"}'}, _settings()
    )
    assert spec.model_map == {}
    assert spec.options == {"image_ref_mode": "data_uri"}
