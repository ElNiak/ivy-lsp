"""Tests for Mirror, MirrorId, and MirrorRegistry."""

import os

import pytest

from ivy_lsp.core.analysis.mirror import Mirror, MirrorId, MirrorRole


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

    def test_to_test_scope(self, sample_mirror):
        scope = sample_mirror.to_test_scope()
        assert scope.test_file == "/ws/quic_tests/quic_server_test.ivy"
        assert scope.include_closure == sample_mirror.include_closure
        assert scope.exported_actions == sample_mirror.exported_actions
        assert scope.imported_actions == sample_mirror.imported_actions
        assert scope.tester_role == "client"

    def test_from_test_scope(self):
        from ivy_lsp.core.analysis.test_scope import TestScope

        scope = TestScope(
            test_file="/ws/test.ivy",
            include_closure=frozenset({"/ws/test.ivy", "/ws/types.ivy"}),
            exported_actions=frozenset({"action_a"}),
            imported_actions=frozenset({"action_b"}),
            tester_role="server",
        )
        mirror = Mirror.from_test_scope(
            scope, protocol="quic", protocol_version="rfc9000"
        )
        assert mirror.id == MirrorId(protocol="quic", entry_stem="test")
        assert mirror.entry_file == "/ws/test.ivy"
        assert mirror.include_closure == scope.include_closure
        assert mirror.role == MirrorRole.SERVER
        assert mirror.protocol_version == "rfc9000"

    def test_from_test_scope_invalid_role_falls_back(self):
        """M1 fix: unknown tester_role falls back to UNKNOWN, not crash."""
        from ivy_lsp.core.analysis.test_scope import TestScope

        scope = TestScope(
            test_file="/ws/test.ivy",
            include_closure=frozenset({"/ws/test.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="proxy",  # invalid role
        )
        mirror = Mirror.from_test_scope(scope, protocol="quic")
        assert mirror.role == MirrorRole.UNKNOWN

    def test_roundtrip_to_test_scope(self, sample_mirror):
        scope = sample_mirror.to_test_scope()
        roundtripped = Mirror.from_test_scope(
            scope,
            protocol=sample_mirror.protocol,
            protocol_version=sample_mirror.protocol_version,
        )
        assert roundtripped.entry_file == sample_mirror.entry_file
        assert roundtripped.include_closure == sample_mirror.include_closure
        assert roundtripped.role == sample_mirror.role
