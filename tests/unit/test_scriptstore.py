"""Ref grammar, filesystem backend and overlay/image precedence."""

import pytest

from adapter.errors import ScriptSourceError
from adapter.scriptstore import ChainStore, DirStore, build_store, parse_ref
from adapter.settings import Settings

SCRIPT = "async def transform(ctx, payload, phase):\n    return payload\n"


class TestParseRef:
    def test_flat_ref_without_version(self):
        ref = parse_ref("vendor_y/mj")
        assert ref.parts == ("vendor_y", "mj")
        assert ref.version is None
        assert ref.stem == "mj"

    def test_versioned_ref(self):
        ref = parse_ref("vendor_y/mj@v1.3")
        assert ref.version == "v1.3"
        assert ref.stem == "mj@v1.3"

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "@v1",
            "vendor/mj@",
            "../etc/passwd",
            "vendor/../../etc/passwd",
            "a/b/c/d/e",
            "vendor/mj\x00",
            "vendor//..//mj",
        ],
    )
    def test_malformed_refs_are_refused(self, bad):
        with pytest.raises(ScriptSourceError):
            parse_ref(bad)


class TestDirStore:
    @pytest.mark.asyncio
    async def test_flat_layout(self, tmp_path):
        (tmp_path / "vendor_y").mkdir()
        (tmp_path / "vendor_y" / "mj@v1.3.py").write_text(SCRIPT)
        store = DirStore(tmp_path)
        assert await store.get(parse_ref("vendor_y/mj@v1.3")) == SCRIPT

    @pytest.mark.asyncio
    async def test_nested_layout(self, tmp_path):
        nested = tmp_path / "vendor_y" / "mj"
        nested.mkdir(parents=True)
        (nested / "v1.3.py").write_text(SCRIPT)
        store = DirStore(tmp_path)
        assert await store.get(parse_ref("vendor_y/mj@v1.3")) == SCRIPT

    @pytest.mark.asyncio
    async def test_miss_returns_none_rather_than_raising(self, tmp_path):
        """A miss must be falsy-but-distinct so the chain can fall through."""
        assert await DirStore(tmp_path).get(parse_ref("nope/absent")) is None

    @pytest.mark.asyncio
    async def test_absent_root_is_not_an_error(self, tmp_path):
        store = DirStore(tmp_path / "never-mounted")
        assert await store.get(parse_ref("vendor_y/mj")) is None

    @pytest.mark.asyncio
    async def test_symlink_escaping_the_root_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text(SCRIPT)
        root = tmp_path / "root"
        root.mkdir()
        (root / "leak.py").symlink_to(outside / "secret.py")
        with pytest.raises(ScriptSourceError, match="escapes the script store"):
            await DirStore(root).get(parse_ref("leak"))


class TestChainPrecedence:
    @pytest.mark.asyncio
    async def test_overlay_wins_and_image_is_the_fallback(self, tmp_path):
        image = tmp_path / "image"
        overlay = tmp_path / "overlay"
        (image / "v").mkdir(parents=True)
        (overlay / "v").mkdir(parents=True)
        (image / "v" / "a.py").write_text("# image a\n")
        (image / "v" / "b.py").write_text("# image b\n")
        (overlay / "v" / "a.py").write_text("# overlay a\n")

        chain = ChainStore([DirStore(overlay, "overlay"), DirStore(image, "image")])
        # Present in both -> overlay, the hot-patch case.
        assert await chain.read(parse_ref("v/a")) == "# overlay a\n"
        # Only in the image -> still resolves, no host dependency required.
        assert await chain.read(parse_ref("v/b")) == "# image b\n"

    @pytest.mark.asyncio
    async def test_total_miss_raises_script_not_found(self, tmp_path):
        chain = ChainStore([DirStore(tmp_path, "image")])
        with pytest.raises(ScriptSourceError) as exc:
            await chain.read(parse_ref("v/missing"))
        assert exc.value.code == "script_not_found"

    def test_default_build_reads_the_image_store_only(self):
        chain = build_store(Settings(script_overlay_dirs=""))
        assert len(chain.backends) == 1
        assert chain.backends[0].name.startswith("image:")

    def test_overlays_are_ordered_ahead_of_the_image_store(self):
        chain = build_store(Settings(script_overlay_dirs="/mnt/one, /mnt/two"))
        labels = [b.name.split(":", 1)[0] for b in chain.backends]
        assert labels == ["overlay", "overlay", "image"]


def _manifest(**kwargs) -> str:
    import json

    return json.dumps({"scripts": {"v/mj": kwargs}})


class TestManifestAliases:
    @pytest.mark.asyncio
    async def test_alias_resolves_to_its_concrete_version(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        (tmp_path / "manifest.json").write_text(
            _manifest(latest="v1.3", aliases={"stable": "v1.3"})
        )
        store = DirStore(tmp_path)
        assert await store.get(parse_ref("v/mj@stable")) == "# v1.3\n"

    @pytest.mark.asyncio
    async def test_alias_chain_is_followed(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        (tmp_path / "manifest.json").write_text(
            _manifest(aliases={"stable": "current", "current": "v1.3"})
        )
        assert await DirStore(tmp_path).get(parse_ref("v/mj@stable")) == "# v1.3\n"

    @pytest.mark.asyncio
    async def test_alias_cycle_terminates(self, tmp_path):
        """A cyclic manifest must not hang the request; it just misses."""
        (tmp_path / "manifest.json").write_text(_manifest(aliases={"a": "b", "b": "a"}))
        store = DirStore(tmp_path)
        assert await store.get(parse_ref("v/mj@a")) is None

    @pytest.mark.asyncio
    async def test_latest_is_an_implicit_alias(self, tmp_path):
        """@latest works off the `latest` field without being listed twice."""
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        (tmp_path / "manifest.json").write_text(_manifest(latest="v1.3"))
        assert await DirStore(tmp_path).get(parse_ref("v/mj@latest")) == "# v1.3\n"

    @pytest.mark.asyncio
    async def test_explicit_alias_overrides_latest_field(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        (tmp_path / "v" / "mj@v1.4.py").write_text("# v1.4\n")
        (tmp_path / "manifest.json").write_text(
            _manifest(latest="v1.3", aliases={"latest": "v1.4"})
        )
        assert await DirStore(tmp_path).get(parse_ref("v/mj@latest")) == "# v1.4\n"

    @pytest.mark.asyncio
    async def test_concrete_ref_needs_no_manifest(self, tmp_path):
        """The manifest is an addition, never a precondition."""
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        assert await DirStore(tmp_path).get(parse_ref("v/mj@v1.3")) == "# v1.3\n"

    @pytest.mark.asyncio
    async def test_unknown_alias_passes_through_unchanged(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@ghost.py").write_text("# ghost\n")
        (tmp_path / "manifest.json").write_text(_manifest(aliases={"stable": "v1.3"}))
        assert await DirStore(tmp_path).get(parse_ref("v/mj@ghost")) == "# ghost\n"

    @pytest.mark.asyncio
    async def test_overlay_manifest_does_not_need_the_image_to_have_one(self, tmp_path):
        image = tmp_path / "image"
        overlay = tmp_path / "overlay"
        (image / "v").mkdir(parents=True)
        (overlay / "v").mkdir(parents=True)
        (image / "v" / "mj@v1.3.py").write_text("# image\n")
        (overlay / "v" / "mj@v1.3.py").write_text("# overlay\n")
        (overlay / "manifest.json").write_text(
            _manifest(latest="v1.3", aliases={"stable": "v1.3"})
        )
        chain = ChainStore([DirStore(overlay, "overlay"), DirStore(image, "image")])
        # Alias resolves in the overlay, which is why it wins here.
        assert await chain.read(parse_ref("v/mj@stable")) == "# overlay\n"
        # A concrete ref the overlay lacks still falls through to the image.
        (image / "v" / "other@v1.py").write_text("# image other\n")
        assert await chain.read(parse_ref("v/other@v1")) == "# image other\n"


class TestManifestDigests:
    @pytest.mark.asyncio
    async def test_digest_pin_refuses_a_mismatch(self, tmp_path):
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# actual\n")
        (tmp_path / "manifest.json").write_text(_manifest(digests={"v1.3": "0" * 64}))
        store = DirStore(tmp_path, pin_digests=True)
        with pytest.raises(ScriptSourceError) as exc:
            await store.get(parse_ref("v/mj@v1.3"))
        assert exc.value.code == "script_integrity_error"

    @pytest.mark.asyncio
    async def test_digest_pin_accepts_a_match(self, tmp_path):
        import hashlib

        text = "# actual\n"
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text(text)
        (tmp_path / "manifest.json").write_text(
            _manifest(digests={"v1.3": hashlib.sha256(text.encode()).hexdigest()})
        )
        store = DirStore(tmp_path, pin_digests=True)
        assert await store.get(parse_ref("v/mj@v1.3")) == text

    @pytest.mark.asyncio
    async def test_digests_are_advisory_by_default(self, tmp_path):
        """Off by default: an unsigned manifest must not break a deployment."""
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# actual\n")
        (tmp_path / "manifest.json").write_text(_manifest(digests={"v1.3": "0" * 64}))
        store = DirStore(tmp_path)
        assert await store.get(parse_ref("v/mj@v1.3")) == "# actual\n"

    @pytest.mark.asyncio
    async def test_pin_applies_to_the_alias_target(self, tmp_path):
        """The digest is checked against the resolved version, not the alias."""
        import hashlib

        text = "# v1.3\n"
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text(text)
        (tmp_path / "manifest.json").write_text(
            _manifest(
                aliases={"stable": "v1.3"},
                digests={"v1.3": hashlib.sha256(text.encode()).hexdigest()},
            )
        )
        store = DirStore(tmp_path, pin_digests=True)
        assert await store.get(parse_ref("v/mj@stable")) == text


class TestMalformedManifestIsIgnored:
    @pytest.mark.parametrize(
        "bad",
        [
            "{ not json",
            "[]",
            '"a string"',
            '{"scripts": []}',
            '{"scripts": {"v/mj": "not an object"}}',
            '{"scripts": {"v/mj": {"aliases": [], "digests": 5}}}',
        ],
    )
    @pytest.mark.asyncio
    async def test_broken_manifest_still_serves_concrete_refs(self, tmp_path, bad):
        """A typo in an optional overlay file must not break resolution."""
        (tmp_path / "v").mkdir()
        (tmp_path / "v" / "mj@v1.3.py").write_text("# v1.3\n")
        (tmp_path / "manifest.json").write_text(bad)
        assert await DirStore(tmp_path).get(parse_ref("v/mj@v1.3")) == "# v1.3\n"

    def test_absent_manifest_is_falsy(self, tmp_path):
        assert not DirStore(tmp_path).manifest

    def test_parsed_manifest_is_truthy(self, tmp_path):
        (tmp_path / "manifest.json").write_text(_manifest(latest="v1.3"))
        assert DirStore(tmp_path).manifest
