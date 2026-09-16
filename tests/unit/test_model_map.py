"""``adapter/modelmap.py``: the channel's model table, parsed and matched.

The module is small on purpose -- the whole feature is one table lookup -- so
what is worth pinning is not the happy path but the decisions that are tempting
to make differently later:

  * an absent table is not an error, and it is not an identity table either;
  * ``*`` is a catch-all, not a pattern, so ``gemini-*`` is refused rather than
    accepted and silently never matched;
  * a request that matches nothing keeps the name it arrived with.

The last one is the zero-regression property the whole design rests on, so it
is asserted from both sides: an empty table, and a table that simply does not
list the model.

``parse_channel`` is exercised here too, because the option is validated there
rather than in a script: a channel-configuration mistake has to be a 400 before
the request reaches an upstream, and the only place that can happen is the
header parser.
"""

from __future__ import annotations

import pytest

from adapter.channel import parse_channel
from adapter.errors import ChannelConfigError
from adapter.modelmap import WILDCARD, parse, resolve
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
    return Settings(_env_file=None, adapter_key_required=False)


# --- parse -------------------------------------------------------------------


def test_an_absent_table_is_the_empty_table():
    """Not declaring the option is the default, not a mistake."""
    assert parse(None) == {}


def test_an_empty_table_is_accepted_and_means_no_mapping():
    assert parse({}) == {}


def test_strings_are_trimmed():
    """Both sides are typed by hand into a header, so padding is a typo."""
    assert parse({" gpt-image-2 ": " doubao-x "}) == {"gpt-image-2": "doubao-x"}


def test_the_bare_wildcard_is_accepted():
    assert parse({WILDCARD: "doubao-x"}) == {WILDCARD: "doubao-x"}


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "an", "object"],
        "not-an-object",
        7,
        True,
    ],
    ids=["list", "string", "number", "boolean"],
)
def test_a_table_that_is_not_an_object_is_refused(raw):
    with pytest.raises(ChannelConfigError) as caught:
        parse(raw)
    assert caught.value.code == "channel_config_error"
    assert caught.value.param == "X-Channel-Options"
    assert "must be a JSON object" in caught.value.message


@pytest.mark.parametrize(
    "raw",
    [
        {"": "doubao-x"},
        {"   ": "doubao-x"},
        {"*": "doubao-x", " * ": "doubao-y"},
        {"gpt-image-2": "a", " gpt-image-2": "b"},
        {"gpt-image-2": ""},
        {"gpt-image-2": "   "},
        {"gpt-image-2": 7},
        {"gpt-image-2": None},
        {"gpt-image-2": ["doubao-x"]},
    ],
    ids=[
        "blank-key",
        "padding-key",
        "wildcard-twice-after-trim",
        "duplicate-after-trim",
        "blank-value",
        "padding-value",
        "numeric-value",
        "null-value",
        "list-value",
    ],
)
def test_an_entry_that_cannot_be_matched_is_refused(raw):
    """A blank key can never match and a non-string value is not a model name.

    Storing either would produce an entry that is present in the table and
    still never applies -- the silent no-op this module exists to avoid.
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse(raw)
    assert caught.value.code == "channel_config_error"


@pytest.mark.parametrize("key", ["gemini-*", "gpt-*", "*gemini", "g*p-t"])
def test_a_partial_glob_is_refused(key):
    """``*`` is a catch-all, not a pattern -- so a pattern must not pass.

    This is the one refusal that is about honesty rather than typing: an
    operator who writes ``{"gemini-*": "x"}`` believes they declared a rule,
    and a table that accepts it and never matches anything is worse than a 400
    naming the key.
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse({key: "doubao-x"})
    assert key in caught.value.message
    assert WILDCARD in caught.value.message


def test_a_repeated_key_after_trimming_is_refused():
    """`"*"` and `" * "` are one key, so the table would hold two catch-alls.

    Trimming is what makes them collide, and the second one silently won before
    this check existed -- one catch-all is the rule, and "which of the two" is
    not a question an operator should have to reason about.
    """
    with pytest.raises(ChannelConfigError) as caught:
        parse({"*": "doubao-x", " * ": "doubao-y"})
    assert "repeats" in caught.value.message


def test_the_wire_is_refused_when_it_repeats_a_key():
    """A repeated JSON key cannot be seen in a `dict` -- the parser keeps the last.

    So the refusal has to happen while the header is being decoded, which is why
    `parse_channel` uses the stdlib decoder with a pairs hook rather than the
    orjson path the request bodies take. `{"*": "a", "*": "b"}` would otherwise
    arrive here as a perfectly ordinary one-entry table.
    """
    for raw in (
        '{"model_map": {"*": "first", "*": "second"}}',
        '{"model": "doubao-seedream-5-0-260128", "model": "doubao-seedream-5-0-pro"}',
    ):
        with pytest.raises(ChannelConfigError) as caught:
            parse_channel({**BASE, "x-channel-options": raw}, _settings())
        assert caught.value.code == "channel_config_error"
        assert "repeats" in caught.value.message


def test_an_exact_entry_beside_the_catch_all_is_still_fine():
    """The positive control: the rule costs a well-formed table nothing."""
    spec = parse_channel(
        {
            **BASE,
            "x-channel-options": '{"model_map": {"*": "doubao-x", "gpt-image-2": "doubao-y"}}',
        },
        _settings(),
    )
    assert spec.model_map == {"*": "doubao-x", "gpt-image-2": "doubao-y"}


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
            "x-channel-options": '{"model_map": {"*": "doubao-x"}, "watermark": false}',
        },
        _settings(),
    )
    assert spec.model_map == {WILDCARD: "doubao-x"}
    # The other options still travel to the script untouched: the mapping is
    # resolved by the adapter, but the bag itself stays the script's.
    assert spec.options["watermark"] is False


def test_a_channel_without_the_option_gets_an_empty_table():
    assert parse_channel(BASE, _settings()).model_map == {}


def test_an_unusable_table_fails_the_channel_rather_than_the_request():
    """The failure mode this pins: 400 before an upstream call, not a silent
    fallback to an unmapped model."""
    with pytest.raises(ChannelConfigError) as caught:
        parse_channel(
            {**BASE, "x-channel-options": '{"model_map": ["not", "a", "table"]}'},
            _settings(),
        )
    assert caught.value.code == "channel_config_error"
    assert caught.value.status == 400
    assert caught.value.param == "X-Channel-Options"


def test_an_unrelated_option_is_still_the_scripts_business():
    """`model_map` is validated because the adapter acts on it. Nothing else is
    -- and this test is the guard against that boundary creeping."""
    spec = parse_channel(
        {**BASE, "x-channel-options": '{"image_ref_mode": "data_uri"}'}, _settings()
    )
    assert spec.model_map == {}
    assert spec.options == {"image_ref_mode": "data_uri"}
