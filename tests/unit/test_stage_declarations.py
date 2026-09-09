"""Unit tests for cascade declarations: the script side (STAGES /
STAGE_FALLBACK) and the control-plane side (X-Stages / X-Stage-Urls /
X-Stage-Timeout).

These run against the parsers directly so the SSRF guard can be switched on;
the integration suite has to allow private addresses to reach its local
vendor, which would mask the check.
"""

from __future__ import annotations

import hashlib

import pytest

from adapter.channel import StageSpec
from adapter.errors import ChannelConfigError, SecurityError
from adapter.script_cache import ScriptCache
from adapter.script_source import ScriptSource
from adapter.settings import Settings


@pytest.fixture
def guarded() -> Settings:
    """Settings with the SSRF guard active, as in production."""
    return Settings(
        adapter_key="k",
        redis_url="",
        minio_endpoint="",
        upstream_allow_private_network=False,
    )


def compile_script(text: str):
    source = ScriptSource(
        text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        origin="test",
    )
    return ScriptCache().load(source)


BODY = (
    "async def transform(ctx, payload, phase):\n"
    "    return {'data': []}\n"
)


class TestStagesDeclaration:
    def test_absent_declaration_means_single_stage(self):
        script = compile_script(BODY)
        assert script.stages == ()
        assert not script.staged

    def test_declared_stages_are_recorded_in_order(self):
        script = compile_script("STAGES = ['preprocess', 'generate']\n" + BODY)
        assert script.stages == ("preprocess", "generate")
        assert script.staged

    def test_empty_list_is_treated_as_no_declaration(self):
        script = compile_script("STAGES = []\n" + BODY)
        assert script.stages == ()

    def test_a_bare_string_is_not_a_stage_list(self):
        # STAGES = "generate" would otherwise iterate into single characters.
        with pytest.raises(SecurityError):
            compile_script("STAGES = 'generate'\n" + BODY)

    def test_duplicate_stage_names_are_rejected(self):
        with pytest.raises(SecurityError):
            compile_script("STAGES = ['a', 'a']\n" + BODY)

    def test_stage_names_are_restricted_to_a_safe_alphabet(self):
        with pytest.raises(SecurityError):
            compile_script("STAGES = ['gen erate']\n" + BODY)

    def test_too_many_stages_are_rejected(self):
        # Each stage is an upstream call; a long list is request amplification.
        many = ", ".join(repr(f"s{i}") for i in range(20))
        with pytest.raises(SecurityError):
            compile_script(f"STAGES = [{many}]\n" + BODY)


class TestFallbackDeclaration:
    def test_absent_declaration_means_nothing_may_degrade(self):
        script = compile_script("STAGES = ['a', 'b']\n" + BODY)
        assert script.fallback == frozenset()
        assert not script.may_degrade("b")

    def test_declared_fallback_stage_may_degrade(self):
        script = compile_script(
            "STAGES = ['generate', 'upscale']\n"
            "STAGE_FALLBACK = ['upscale']\n" + BODY
        )
        assert script.may_degrade("upscale")
        assert not script.may_degrade("generate")

    def test_fallback_naming_an_undeclared_stage_is_rejected(self):
        with pytest.raises(SecurityError):
            compile_script(
                "STAGES = ['generate']\nSTAGE_FALLBACK = ['upscale']\n" + BODY
            )

    def test_fallback_without_stages_is_rejected(self):
        with pytest.raises(SecurityError):
            compile_script("STAGE_FALLBACK = ['upscale']\n" + BODY)

    def test_the_first_stage_cannot_be_degradable(self):
        # There is no prior artefact to fall back to, so this would return
        # nothing while still reporting success.
        with pytest.raises(SecurityError):
            compile_script(
                "STAGES = ['generate', 'upscale']\n"
                "STAGE_FALLBACK = ['generate']\n" + BODY
            )


class TestStageUrlHeader:
    def test_pairs_are_parsed(self, guarded):
        spec = StageSpec.parse(
            {"x-stage-urls": "generate=https://a.test/t2i,upscale=https://b.test/sr"},
            guarded,
        )
        assert spec.urls["generate"] == "https://a.test/t2i"
        assert spec.urls["upscale"] == "https://b.test/sr"

    def test_cloud_metadata_endpoint_is_refused(self, guarded):
        # The header is caller-supplied, so it is an SSRF vector.
        with pytest.raises(ChannelConfigError):
            StageSpec.parse(
                {"x-stage-urls": "generate=http://169.254.169.254/latest/meta-data"},
                guarded,
            )

    def test_loopback_is_refused_when_private_access_is_off(self, guarded):
        with pytest.raises(ChannelConfigError):
            StageSpec.parse(
                {"x-stage-urls": "generate=http://127.0.0.1:8080/x"}, guarded
            )

    def test_private_range_is_refused(self, guarded):
        with pytest.raises(ChannelConfigError):
            StageSpec.parse(
                {"x-stage-urls": "generate=http://10.0.0.5/internal"}, guarded
            )

    def test_malformed_pair_is_refused(self, guarded):
        with pytest.raises(ChannelConfigError):
            StageSpec.parse({"x-stage-urls": "https://a.test/t2i"}, guarded)

    def test_repeated_stage_key_is_refused(self, guarded):
        with pytest.raises(ChannelConfigError):
            StageSpec.parse(
                {"x-stage-urls": "a=https://x.test/1,a=https://y.test/2"}, guarded
            )

    def test_url_for_a_stage_absent_from_x_stages_is_refused(self, guarded):
        # Catches a misspelled stage name that would otherwise misroute.
        with pytest.raises(ChannelConfigError):
            StageSpec.parse(
                {
                    "x-stages": "generate",
                    "x-stage-urls": "upscal=https://b.test/sr",
                },
                guarded,
            )


class TestStageTimeoutHeader:
    def test_total_is_parsed(self, guarded):
        assert StageSpec.parse({"x-stage-timeout": "total=300"}, guarded).budget == 300.0

    def test_absent_header_leaves_the_budget_unset(self, guarded):
        assert StageSpec.parse({}, guarded).budget is None

    @pytest.mark.parametrize(
        "value", ["per_stage=30", "total=abc", "total=-5", "total=0"]
    )
    def test_invalid_values_are_refused(self, guarded, value):
        with pytest.raises(ChannelConfigError):
            StageSpec.parse({"x-stage-timeout": value}, guarded)
