"""Tests for Mirror, MirrorId, and MirrorRegistry."""

import os

import pytest

from ivy_lsp.core.analysis.mirror import Mirror, MirrorId, MirrorRegistry, MirrorRole


class TestMirrorId:
    def test_create_from_fields(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_str_representation(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert str(mid) == "quic::quic_server_test_stream"

    def test_from_test_file(self):
        mid = MirrorId.from_test_file(
            "/path/to/quic_server_test_stream.ivy", protocol="quic"
        )
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_from_test_file_strips_ivy_extension(self):
        mid = MirrorId.from_test_file("/any/path/foo_test.ivy", protocol="minip")
        assert mid.entry_stem == "foo_test"

    def test_hashable_and_eq(self):
        a = MirrorId(protocol="quic", entry_stem="test_a")
        b = MirrorId(protocol="quic", entry_stem="test_a")
        c = MirrorId(protocol="quic", entry_stem="test_b")
        assert a == b
        assert a != c
        assert {a, b, c} == {a, c}

    def test_from_test_file_no_extension(self):
        mid = MirrorId.from_test_file("/path/to/test_file", protocol="quic")
        assert mid.entry_stem == "test_file"

    def test_frozen(self):
        mid = MirrorId(protocol="quic", entry_stem="test")
        with pytest.raises(AttributeError):
            mid.protocol = "other"


class TestMirrorRole:
    def test_values(self):
        assert MirrorRole.CLIENT.value == "client"
        assert MirrorRole.SERVER.value == "server"
        assert MirrorRole.MIM.value == "mim"
        assert MirrorRole.UNKNOWN.value == "unknown"

    def test_from_string(self):
        assert MirrorRole("client") == MirrorRole.CLIENT


class TestMirror:
    @pytest.fixture
    def sample_mirror(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="quic_server_test"),
            entry_file="/ws/quic_tests/quic_server_test.ivy",
            include_closure=frozenset(
                {
                    "/ws/quic_tests/quic_server_test.ivy",
                    "/ws/quic_stack/quic_types.ivy",
                    "/ws/quic_stack/quic_frame.ivy",
                }
            ),
            exported_actions=frozenset({"send_frame", "recv_frame"}),
            imported_actions=frozenset({"accept_connection"}),
            role=MirrorRole.CLIENT,
            protocol_version="rfc9000",
        )

    def test_is_file_in_scope(self, sample_mirror):
        assert sample_mirror.is_file_in_scope("/ws/quic_stack/quic_types.ivy")
        assert not sample_mirror.is_file_in_scope("/ws/other/unrelated.ivy")

    def test_file_count(self, sample_mirror):
        assert sample_mirror.file_count() == 3

    def test_frozen(self, sample_mirror):
        with pytest.raises(AttributeError):
            sample_mirror.protocol = "other"


class TestMirrorRegistry:
    @pytest.fixture
    def registry(self):
        return MirrorRegistry()

    @pytest.fixture
    def mirror_a(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="server_test"),
            entry_file="/ws/server_test.ivy",
            include_closure=frozenset(
                {
                    "/ws/server_test.ivy",
                    "/ws/types.ivy",
                    "/ws/frame.ivy",
                }
            ),
            exported_actions=frozenset({"send"}),
            imported_actions=frozenset({"recv"}),
            role=MirrorRole.CLIENT,
        )

    @pytest.fixture
    def mirror_b(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="client_test"),
            entry_file="/ws/client_test.ivy",
            include_closure=frozenset(
                {
                    "/ws/client_test.ivy",
                    "/ws/types.ivy",
                    "/ws/connection.ivy",
                }
            ),
            exported_actions=frozenset({"recv"}),
            imported_actions=frozenset({"send"}),
            role=MirrorRole.SERVER,
        )

    def test_register_and_get(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get(mirror_a.id) is mirror_a

    def test_get_missing_returns_none(self, registry):
        mid = MirrorId(protocol="quic", entry_stem="nonexistent")
        assert registry.get(mid) is None

    def test_get_by_entry_file(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get_by_entry_file("/ws/server_test.ivy") is mirror_a
        assert registry.get_by_entry_file("/ws/nonexistent.ivy") is None

    def test_get_mirrors_for_file_shared(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # types.ivy is in both mirrors
        mirrors = registry.get_mirrors_for_file("/ws/types.ivy")
        ids = {m.id for m in mirrors}
        assert ids == {mirror_a.id, mirror_b.id}

    def test_get_mirrors_for_file_exclusive(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # frame.ivy only in mirror_a
        mirrors = registry.get_mirrors_for_file("/ws/frame.ivy")
        assert len(mirrors) == 1
        assert mirrors[0].id == mirror_a.id

    def test_get_mirrors_for_file_unknown(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get_mirrors_for_file("/ws/unknown.ivy") == []

    def test_get_mirrors_for_protocol(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        quic = registry.get_mirrors_for_protocol("quic")
        assert len(quic) == 2
        assert registry.get_mirrors_for_protocol("minip") == []

    def test_all_mirrors(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        assert len(registry.all_mirrors()) == 2

    def test_invalidate_file(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # types.ivy is shared — both mirrors affected
        affected = registry.invalidate_file("/ws/types.ivy")
        assert affected == {mirror_a.id, mirror_b.id}

    def test_invalidate_file_exclusive(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        affected = registry.invalidate_file("/ws/frame.ivy")
        assert affected == {mirror_a.id}

    def test_invalidate_file_unknown(self, registry):
        assert registry.invalidate_file("/ws/unknown.ivy") == set()

    def test_clear(self, registry, mirror_a):
        registry.register(mirror_a)
        registry.clear()
        assert registry.all_mirrors() == []
        assert registry.get(mirror_a.id) is None

    def test_reregister_cleans_stale_reverse_index(self, registry, mirror_a):
        """C1 fix: re-registering with changed closure removes stale entries."""
        registry.register(mirror_a)  # closure includes /ws/frame.ivy
        updated = Mirror(
            id=mirror_a.id,
            entry_file=mirror_a.entry_file,
            include_closure=frozenset({"/ws/server_test.ivy", "/ws/types.ivy"}),
            exported_actions=mirror_a.exported_actions,
            imported_actions=mirror_a.imported_actions,
            role=mirror_a.role,
        )
        registry.register(updated)
        # frame.ivy was removed from closure — should not be found
        assert registry.get_mirrors_for_file("/ws/frame.ivy") == []
        # types.ivy still in closure — should still be found
        assert len(registry.get_mirrors_for_file("/ws/types.ivy")) == 1

    def test_to_snapshot_dict(self, registry, mirror_a, mirror_b):
        """H3 fix: snapshot extraction for Plan 2 IndexSnapshot."""
        registry.register(mirror_a)
        registry.register(mirror_b)
        snap = registry.to_snapshot_dict()
        assert len(snap) == 2
        assert mirror_a.id in snap
        # Snapshot is a copy — mutations don't affect registry
        snap.clear()
        assert len(registry.all_mirrors()) == 2


class TestMirrorIntegration:
    """Test that _compute_test_scopes populates the mirror registry."""

    def test_compute_test_scopes_populates_registry(self, tmp_path):
        """Verify mirror registry populated after _compute_test_scopes.

        Tests at the ScopeManagerMixin level to avoid needing a full parser.
        We construct a WorkspaceIndexer with a None parser (fast-index only).
        """
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer

        # Create a minimal workspace with a test file that has exports
        test_file = tmp_path / "my_test.ivy"
        test_file.write_text(
            "#lang ivy1.7\n\nexport action step\nimport action recv\n"
            "action step\naction recv\n"
        )
        types_file = tmp_path / "types.ivy"
        types_file.write_text("#lang ivy1.7\n\ntype t\n")

        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(
            workspace_root=str(tmp_path),
            parser=None,  # No deep parsing needed
            resolver=resolver,
        )
        indexer.index_workspace()

        # The mirror registry should have been populated
        registry = indexer.mirror_registry
        assert registry is not None
        mirrors = registry.all_mirrors()
        # At least one mirror should exist (for the test file with exports)
        assert len(mirrors) >= 1
        # Protocol defaults to "unknown" (resolved by NctWorkspace in Plan 3)
        assert all(m.protocol == "unknown" for m in mirrors)
