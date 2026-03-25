"""Tests for TestScope serialization (to_dict / from_dict)."""

from ivy_lsp.core.analysis.test_scope import TestScope


class TestTestScopeSerialization:
    def test_to_dict(self):
        scope = TestScope(
            test_file="/opt/ivy/quic_tests/quic_server_test_stream.ivy",
            include_closure=frozenset(
                {
                    "/opt/ivy/quic_tests/quic_server_test_stream.ivy",
                    "/opt/ivy/quic_stack/quic_types.ivy",
                    "/opt/ivy/quic_stack/quic_frame.ivy",
                }
            ),
            exported_actions=frozenset({"quic.send", "quic.recv"}),
            imported_actions=frozenset({"tls.handshake"}),
            tester_role="client",
        )
        d = scope.to_dict()
        assert d["test"] == "quic_server_test_stream"
        assert d["entry_file"] == "/opt/ivy/quic_tests/quic_server_test_stream.ivy"
        assert d["role"] == "client"
        assert d["transitive_includes"] == sorted(scope.include_closure)
        assert d["exported_actions"] == ["quic.recv", "quic.send"]
        assert d["imported_actions"] == ["tls.handshake"]
        assert d["file_count"] == 3

    def test_from_dict(self):
        d = {
            "test": "quic_server_test_stream",
            "entry_file": "/opt/ivy/quic_tests/quic_server_test_stream.ivy",
            "role": "client",
            "transitive_includes": ["/opt/ivy/quic_stack/quic_types.ivy"],
            "exported_actions": ["quic.send"],
            "imported_actions": ["tls.handshake"],
            "file_count": 1,
        }
        scope = TestScope.from_dict(d)
        assert scope.test_file == "/opt/ivy/quic_tests/quic_server_test_stream.ivy"
        assert scope.tester_role == "client"
        assert scope.include_closure == frozenset(
            {"/opt/ivy/quic_stack/quic_types.ivy"}
        )
        assert scope.exported_actions == frozenset({"quic.send"})
        assert scope.imported_actions == frozenset({"tls.handshake"})

    def test_roundtrip(self):
        scope = TestScope(
            test_file="/test/quic_server_test.ivy",
            include_closure=frozenset(
                {"/test/quic_server_test.ivy", "/test/types.ivy", "/test/frame.ivy"}
            ),
            exported_actions=frozenset({"quic.send", "quic.recv"}),
            imported_actions=frozenset({"tls.handshake", "tls.close"}),
            tester_role="client",
        )
        restored = TestScope.from_dict(scope.to_dict())
        assert restored.test_file == scope.test_file
        assert restored.include_closure == scope.include_closure
        assert restored.exported_actions == scope.exported_actions
        assert restored.imported_actions == scope.imported_actions
        assert restored.tester_role == scope.tester_role

    def test_roundtrip_empty_collections(self):
        scope = TestScope(
            test_file="/test/empty_test.ivy",
            include_closure=frozenset(),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="unknown",
        )
        restored = TestScope.from_dict(scope.to_dict())
        assert restored.test_file == scope.test_file
        assert restored.include_closure == frozenset()
        assert restored.exported_actions == frozenset()
        assert restored.imported_actions == frozenset()
        assert restored.tester_role == "unknown"

    def test_from_dict_defaults(self):
        """Minimal dict with only entry_file should use safe defaults."""
        d = {"entry_file": "/test/minimal.ivy"}
        scope = TestScope.from_dict(d)
        assert scope.test_file == "/test/minimal.ivy"
        assert scope.include_closure == frozenset()
        assert scope.exported_actions == frozenset()
        assert scope.imported_actions == frozenset()
        assert scope.tester_role == "unknown"

    def test_to_dict_test_name_strips_extension(self):
        scope = TestScope(
            test_file="/some/path/my_test.ivy",
            include_closure=frozenset(),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="server",
        )
        d = scope.to_dict()
        assert d["test"] == "my_test"
        assert ".ivy" not in d["test"]
