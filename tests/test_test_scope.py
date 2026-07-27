"""Tests for TestScope computation and role detection."""

import pytest

from ivy_lsp.core.analysis.test_scope import TestScope, detect_test_role


class TestDetectTestRole:
    def test_server_behavior_means_client_tester(self):
        closure = frozenset(
            {
                "/test/quic_server_test.ivy",
                "/test/ivy_quic_server_behavior.ivy",
                "/test/quic_types.ivy",
            }
        )
        assert detect_test_role(closure) == "client"

    def test_client_behavior_means_server_tester(self):
        closure = frozenset(
            {
                "/test/quic_client_test.ivy",
                "/test/ivy_quic_client_behavior.ivy",
            }
        )
        assert detect_test_role(closure) == "server"

    def test_mim_behavior(self):
        closure = frozenset(
            {
                "/test/quic_mim_test.ivy",
                "/test/ivy_quic_mim_behavior.ivy",
            }
        )
        assert detect_test_role(closure) == "mim"

    def test_unknown_when_no_behavior_file(self):
        closure = frozenset({"/test/quic_types.ivy", "/test/quic_frame.ivy"})
        assert detect_test_role(closure) == "unknown"

    def test_empty_closure(self):
        assert detect_test_role(frozenset()) == "unknown"

    def test_mim_entity_file_not_misclassified(self):
        """Entity file ivy_quic_mim.ivy should NOT trigger mim classification."""
        closure = frozenset(
            {
                "/test/quic_client_test.ivy",
                "/test/ivy_quic_server_behavior.ivy",
                "/test/ivy_quic_mim.ivy",  # entity, not behavior
                "/test/quic_types.ivy",
            }
        )
        assert detect_test_role(closure) == "client"

    def test_mim_behavior_takes_priority(self):
        """When mim_behavior AND server_behavior present, mim wins."""
        closure = frozenset(
            {
                "/test/quic_client_test_mim.ivy",
                "/test/ivy_quic_mim_behavior.ivy",
                "/test/ivy_quic_server_behavior.ivy",
                "/test/ivy_quic_mim.ivy",
            }
        )
        assert detect_test_role(closure) == "mim"

    def test_man_in_the_middle_naming(self):
        """APT-style man_in_the_middle naming should classify as mim."""
        closure = frozenset(
            {
                "/test/minip_mim_test_delay.ivy",
                "/test/ivy_man_in_the_middle_minip_behavior.ivy",
            }
        )
        assert detect_test_role(closure) == "mim"

    def test_entity_and_shim_mim_ignored(self):
        """Both entity and shim mim files should be ignored for role detection."""
        closure = frozenset(
            {
                "/test/quic_server_test.ivy",
                "/test/ivy_quic_server_behavior.ivy",
                "/test/ivy_quic_mim.ivy",  # entity
                "/test/ivy_quic_shim_mim.ivy",  # shim
                "/test/ivy_quic_victim.ivy",
            }
        )
        assert detect_test_role(closure) == "client"

    def test_deterministic_across_runs(self):
        """Result must be identical regardless of FrozenSet iteration order."""
        closure = frozenset(
            {
                "/test/quic_client_test.ivy",
                "/test/ivy_quic_server_behavior.ivy",
                "/test/ivy_quic_mim.ivy",
                "/test/ivy_quic_shim_mim.ivy",
                "/test/quic_types.ivy",
                "/test/quic_frame.ivy",
                "/test/quic_packet.ivy",
                "/test/attack_connection.ivy",
            }
        )
        results = {detect_test_role(closure) for _ in range(100)}
        assert results == {"client"}

    def test_realistic_quic_client_closure(self):
        """Realistic QUIC client test closure with all common entity/shim files."""
        closure = frozenset(
            {
                "quic_tests/client_tests/quic_client_test.ivy",
                "quic_entities_behavior/ivy_quic_server_behavior.ivy",
                "quic_entities_behavior/quic_endpoint.ivy",
                "quic_entities/ivy_quic_client.ivy",
                "quic_entities/ivy_quic_server.ivy",
                "quic_entities/ivy_quic_mim.ivy",
                "quic_entities/ivy_quic_victim.ivy",
                "quic_entities/ivy_quic_client_server.ivy",
                "quic_shims/ivy_quic_shim_server.ivy",
                "quic_shims/quic_shim.ivy",
                "quic_attacks_stack/attack_connection.ivy",
                "quic_attacks_stack/forged_quic_packet.ivy",
                "quic_stack/quic_types.ivy",
                "quic_stack/quic_frame.ivy",
                "quic_stack/quic_packet.ivy",
                "quic_stack/quic_connection.ivy",
            }
        )
        assert detect_test_role(closure) == "client"


class TestTestScopeCreation:
    def test_create_scope(self):
        scope = TestScope(
            test_file="/test/quic_server_test.ivy",
            include_closure=frozenset(
                {"/test/quic_server_test.ivy", "/test/types.ivy"}
            ),
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
