"""Tests for TestScope computation and role detection."""
import pytest
from ivy_lsp.analysis.test_scope import TestScope, detect_test_role


class TestDetectTestRole:
    def test_server_behavior_means_client_tester(self):
        closure = frozenset({
            "/test/quic_server_test.ivy",
            "/test/ivy_quic_server_behavior.ivy",
            "/test/quic_types.ivy",
        })
        assert detect_test_role(closure) == "client"

    def test_client_behavior_means_server_tester(self):
        closure = frozenset({
            "/test/quic_client_test.ivy",
            "/test/ivy_quic_client_behavior.ivy",
        })
        assert detect_test_role(closure) == "server"

    def test_mim_behavior(self):
        closure = frozenset({
            "/test/quic_mim_test.ivy",
            "/test/ivy_quic_mim_behavior.ivy",
        })
        assert detect_test_role(closure) == "mim"

    def test_unknown_when_no_behavior_file(self):
        closure = frozenset({"/test/quic_types.ivy", "/test/quic_frame.ivy"})
        assert detect_test_role(closure) == "unknown"

    def test_empty_closure(self):
        assert detect_test_role(frozenset()) == "unknown"


class TestTestScopeCreation:
    def test_create_scope(self):
        scope = TestScope(
            test_file="/test/quic_server_test.ivy",
            include_closure=frozenset({"/test/quic_server_test.ivy", "/test/types.ivy"}),
            exported_actions=frozenset({"quic.send", "quic.recv"}),
            imported_actions=frozenset({"tls.handshake"}),
            tester_role="client",
        )
        assert scope.test_file == "/test/quic_server_test.ivy"
        assert len(scope.include_closure) == 2
        assert "quic.send" in scope.exported_actions
        assert scope.tester_role == "client"

    def test_action_in_scope(self):
        scope = TestScope(
            test_file="/test/test.ivy",
            include_closure=frozenset({"/test/test.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        assert scope.is_action_exported("quic.send") is True
        assert scope.is_action_exported("quic.recv") is False

    def test_file_in_scope(self):
        scope = TestScope(
            test_file="/test/test.ivy",
            include_closure=frozenset({"/test/test.ivy", "/test/types.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        assert scope.is_file_in_scope("/test/types.ivy") is True
        assert scope.is_file_in_scope("/other/file.ivy") is False
