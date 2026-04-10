"""Tests for workspace-scoped diagnostic filtering."""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import SymbolNode


@pytest.mark.unit
class TestShadowDiagnosticScoping:
    """Bug 3: Shadow diagnostics must not cross workspace boundaries."""

    def _make_model(self, nodes):
        model = SemanticModel()
        for n in nodes:
            model.add_node(n)
        return model

    def test_cross_protocol_shadow_suppressed(self):
        """Two symbols with same name in different protocols produce no shadow diagnostic."""
        from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics

        model = self._make_model(
            [
                SymbolNode(
                    id="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy:61:show_connected",
                    name="show_connected",
                    qualified_name="show_connected",
                    kind="action",
                    file="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy",
                    line=60,
                ),
                SymbolNode(
                    id="/ws/protocol-testing/quic/quic_tests/quic_test.ivy:256:show_connected",
                    name="show_connected",
                    qualified_name="show_connected",
                    kind="action",
                    file="/ws/protocol-testing/quic/quic_tests/quic_test.ivy",
                    line=255,
                ),
            ]
        )

        source = "#lang ivy1.7\n" + "\n" * 60 + "action show_connected\n"
        filepath = "/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy"

        diags = compute_semantic_diagnostics(model, filepath, source)
        shadow_diags = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert len(shadow_diags) == 0

    def test_same_protocol_shadow_reported(self):
        """Two symbols with same name in the same protocol produce a shadow diagnostic."""
        from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics

        model = self._make_model(
            [
                SymbolNode(
                    id="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy:61:show_connected",
                    name="show_connected",
                    qualified_name="show_connected",
                    kind="action",
                    file="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy",
                    line=60,
                ),
                SymbolNode(
                    id="/ws/protocol-testing/bgp/bgp_utils/helpers.ivy:10:show_connected",
                    name="show_connected",
                    qualified_name="show_connected",
                    kind="action",
                    file="/ws/protocol-testing/bgp/bgp_utils/helpers.ivy",
                    line=9,
                ),
            ]
        )

        source = "#lang ivy1.7\n" + "\n" * 60 + "action show_connected\n"
        filepath = "/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy"

        diags = compute_semantic_diagnostics(model, filepath, source)
        shadow_diags = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert len(shadow_diags) == 1
