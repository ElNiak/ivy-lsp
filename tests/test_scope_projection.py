"""Tests for ScopeProjection frozen dataclass."""

import pytest

from ivy_lsp.core.workspace.active_workspace import ScopeProjection


def test_scope_projection_visibility():
    proj = ScopeProjection(
        active_layers=frozenset({"quic", "quic_tests"}),
        file_to_layer={"a.ivy": "quic", "b.ivy": "apt", "c.ivy": "quic_tests"},
    )
    assert proj.is_visible("a.ivy") is True
    assert proj.is_visible("b.ivy") is False
    assert proj.is_visible("c.ivy") is True
    assert proj.is_visible("unknown.ivy") is True  # not in mapping -> visible


def test_scope_projection_empty_layers_all_visible():
    proj = ScopeProjection(
        active_layers=frozenset(),
        file_to_layer={"a.ivy": "quic"},
    )
    # Empty active_layers means no restriction -> everything visible
    assert proj.is_visible("a.ivy") is True


def test_scope_projection_is_frozen():
    proj = ScopeProjection(
        active_layers=frozenset({"quic"}),
        file_to_layer={},
    )
    with pytest.raises(AttributeError):
        proj.active_layers = frozenset({"other"})
