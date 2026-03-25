"""Tests for ivy_quality context-aware suggestions (P8 fix).

Verifies that _resolve_scope derives endpoint-mirror scope from filePath
when testFile is not explicitly provided.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestResolveScopeFilePath:
    """Unit tests for _resolve_scope filePath derivation."""

    def test_file_path_resolves_to_scope(self):
        """When filePath is provided without testFile, scope is derived from file."""
        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/fake/test_quic.ivy",
            include_closure=frozenset({"/fake/quic_types.ivy", "/fake/test_quic.ivy"}),
            exported_actions=frozenset({"quic.send"}),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)

        from ivy_lsp.features.visualization import _resolve_scope

        result = _resolve_scope(graph, {"filePath": "/fake/quic_types.ivy"})

        assert result["scoped"] is True
        assert result["testFile"] == "/fake/test_quic.ivy"
        assert result["_scope"] is scope

    def test_file_path_not_in_any_scope_falls_back_to_active(self):
        """When filePath doesn't match any scope, fall back to active scope."""
        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/fake/test_quic.ivy",
            include_closure=frozenset({"/fake/quic_types.ivy", "/fake/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        graph.set_active_test("/fake/test_quic.ivy")

        from ivy_lsp.features.visualization import _resolve_scope

        result = _resolve_scope(graph, {"filePath": "/fake/unrelated.ivy"})

        assert result["scoped"] is True
        assert result["testFile"] == "/fake/test_quic.ivy"
        assert result["_scope"] is scope

    def test_file_path_not_in_any_scope_no_active(self):
        """When filePath doesn't match and no active scope, result is unscoped."""
        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/fake/test_quic.ivy",
            include_closure=frozenset({"/fake/quic_types.ivy", "/fake/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)

        from ivy_lsp.features.visualization import _resolve_scope

        result = _resolve_scope(graph, {"filePath": "/fake/unrelated.ivy"})

        assert result["scoped"] is False
        assert result["testFile"] is None

    def test_test_file_param_takes_precedence_over_file_path(self):
        """When testFile is provided explicitly, filePath is not used for scope."""
        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        scope_a = TestScope(
            test_file="/fake/test_a.ivy",
            include_closure=frozenset({"/fake/common.ivy", "/fake/test_a.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        scope_b = TestScope(
            test_file="/fake/test_b.ivy",
            include_closure=frozenset({"/fake/common.ivy", "/fake/test_b.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="server",
        )
        graph.register_test_scope(scope_a)
        graph.register_test_scope(scope_b)

        from ivy_lsp.features.visualization import _resolve_scope

        result = _resolve_scope(
            graph,
            {"testFile": "/fake/test_b.ivy", "filePath": "/fake/common.ivy"},
        )

        assert result["scoped"] is True
        assert result["testFile"] == "/fake/test_b.ivy"
        assert result["_scope"] is scope_b

    def test_empty_file_path_falls_back_to_active(self):
        """Empty filePath string should not attempt file-based resolution."""
        from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel, TestScope

        graph = ScopedRequirementModel()
        scope = TestScope(
            test_file="/fake/test_quic.ivy",
            include_closure=frozenset({"/fake/test_quic.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="client",
        )
        graph.register_test_scope(scope)
        graph.set_active_test("/fake/test_quic.ivy")

        from ivy_lsp.features.visualization import _resolve_scope

        result = _resolve_scope(graph, {"filePath": ""})

        assert result["scoped"] is True
        assert result["testFile"] == "/fake/test_quic.ivy"
        assert result["_scope"] is scope


class TestSmartSuggestionsContextFiltering:
    """Integration tests: handle_smart_suggestions uses filePath for scoping."""

    def test_file_path_resolves_to_scope_in_handler(self):
        """When filePath is provided without actionName, suggestions are scoped."""
        from ivy_lsp.features.visualization import handle_smart_suggestions

        server = MagicMock()
        graph = MagicMock()
        graph.snapshot.return_value = MagicMock(
            actions={},
            state_vars={},
            incoming={},
            outgoing={},
            get_requirements_for_action=MagicMock(return_value=[]),
        )

        with patch(
            "ivy_lsp.features.visualization._get_requirement_graph",
            return_value=graph,
        ):
            result = handle_smart_suggestions(
                server,
                {"filePath": "/fake/quic_types.ivy", "line": 10},
            )

        assert "suggestions" in result

    def test_non_scoped_graph_returns_all_suggestions(self):
        """When graph is not a ScopedRequirementModel, no scoping is applied."""
        from ivy_lsp.core.analysis.requirement_graph import RequirementGraph
        from ivy_lsp.features.visualization import _resolve_scope

        graph = RequirementGraph()

        result = _resolve_scope(graph, {"filePath": "/fake/quic_types.ivy"})

        assert result["scoped"] is False
        assert result["testFile"] is None
