"""Tests for ivy_lsp.active_workspace module."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from ivy_lsp.active_workspace import ActiveWorkspace

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_file_to_layer():
    """Simple file-to-layer mapping for tests."""
    return {
        "/workspace/quic/quic_types.ivy": "quic",
        "/workspace/quic/quic_packet.ivy": "quic",
        "/workspace/quic_tests/test_client.ivy": "quic_tests",
        "/workspace/quic_tests/test_server.ivy": "quic_tests",
        "/workspace/apt/apt_model.ivy": "apt",
        "/workspace/minip/minip_types.ivy": "minip",
    }


@pytest.fixture
def workspace_groups():
    """Workspace groups mapping group name to list of layer IDs."""
    return {
        "quic": ["quic", "quic_tests"],
        "apt": ["apt"],
        "minip": ["minip"],
    }


@pytest.fixture
def workspace_layers_list():
    """List of WorkspaceLayer-like objects (dicts for simplicity)."""
    from ivy_lsp.workspace_detection import WorkspaceLayer

    return [
        WorkspaceLayer(id="quic", include_paths=["quic"], depends_on=[]),
        WorkspaceLayer(
            id="quic_tests", include_paths=["quic_tests"], depends_on=["quic"]
        ),
        WorkspaceLayer(id="apt", include_paths=["apt"], depends_on=["quic"]),
        WorkspaceLayer(id="minip", include_paths=["minip"], depends_on=[]),
    ]


@pytest.fixture
def active_quic_workspace(workspace_groups):
    """An ActiveWorkspace set to the quic group."""
    return ActiveWorkspace(
        active_group="quic",
        active_layers={"quic", "quic_tests"},
        active_tests=[],
        granularity="protocol",
        set_by="explicit",
    )


# ---------------------------------------------------------------------------
# Test: cleared() factory
# ---------------------------------------------------------------------------


class TestClearedWorkspace:
    def test_cleared_is_not_set(self):
        ws = ActiveWorkspace.cleared()
        assert ws.granularity == "none"
        assert ws.active_group is None
        assert len(ws.active_layers) == 0
        assert ws.set_by == "cleared"

    def test_is_set_false_when_cleared(self):
        ws = ActiveWorkspace.cleared()
        assert ws.is_set() is False

    def test_cleared_workspace_allows_everything(self, simple_file_to_layer):
        ws = ActiveWorkspace.cleared()
        allowed, reason = ws.is_file_allowed(
            "/workspace/apt/apt_model.ivy", simple_file_to_layer
        )
        assert allowed is True
        # No reason needed when cleared
        assert reason == "" or reason is not None  # any value is fine


# ---------------------------------------------------------------------------
# Test: is_set()
# ---------------------------------------------------------------------------


class TestIsSet:
    def test_is_set_true_when_active(self):
        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic", "quic_tests"},
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )
        assert ws.is_set() is True

    def test_is_set_false_when_no_layers(self):
        ws = ActiveWorkspace(
            active_group="quic",
            active_layers=set(),
            active_tests=[],
            granularity="protocol",
            set_by="explicit",
        )
        assert ws.is_set() is False

    def test_is_set_false_when_granularity_none(self):
        ws = ActiveWorkspace(
            active_group=None,
            active_layers={"quic"},
            active_tests=[],
            granularity="none",
            set_by="cleared",
        )
        assert ws.is_set() is False


# ---------------------------------------------------------------------------
# Test: is_file_allowed()
# ---------------------------------------------------------------------------


class TestIsFileAllowed:
    def test_set_workspace_allows_in_scope_file(
        self, active_quic_workspace, simple_file_to_layer
    ):
        allowed, reason = active_quic_workspace.is_file_allowed(
            "/workspace/quic/quic_types.ivy", simple_file_to_layer
        )
        assert allowed is True
        assert "quic" in reason

    def test_set_workspace_blocks_out_of_scope(
        self, active_quic_workspace, simple_file_to_layer
    ):
        allowed, reason = active_quic_workspace.is_file_allowed(
            "/workspace/apt/apt_model.ivy", simple_file_to_layer
        )
        assert allowed is False
        assert "apt" in reason
        assert "quic" in reason  # mentions active group or active layers

    def test_stdlib_always_allowed(self, active_quic_workspace, simple_file_to_layer):
        """Files with 'ivy/include' in path are stdlib — always pass through."""
        stdlib_path = "/opt/ivy/include/1.7/order.ivy"
        allowed, reason = active_quic_workspace.is_file_allowed(
            stdlib_path, simple_file_to_layer
        )
        assert allowed is True
        assert reason == "stdlib"

    def test_unlayered_file_allowed(self, active_quic_workspace, simple_file_to_layer):
        """A file not tracked in file_to_layer passes with fail-open semantics."""
        allowed, reason = active_quic_workspace.is_file_allowed(
            "/workspace/unknown_layer/some_file.ivy", simple_file_to_layer
        )
        assert allowed is True
        assert reason == "unlayered"

    def test_cleared_workspace_allows_all_files(self, simple_file_to_layer):
        ws = ActiveWorkspace.cleared()
        for filepath in simple_file_to_layer:
            allowed, _ = ws.is_file_allowed(filepath, simple_file_to_layer)
            assert allowed is True, f"Expected {filepath} to be allowed when cleared"

    def test_quic_tests_layer_allowed_in_quic_group(
        self, active_quic_workspace, simple_file_to_layer
    ):
        allowed, reason = active_quic_workspace.is_file_allowed(
            "/workspace/quic_tests/test_client.ivy", simple_file_to_layer
        )
        assert allowed is True
        assert "quic_tests" in reason

    def test_minip_blocked_when_quic_active(
        self, active_quic_workspace, simple_file_to_layer
    ):
        allowed, reason = active_quic_workspace.is_file_allowed(
            "/workspace/minip/minip_types.ivy", simple_file_to_layer
        )
        assert allowed is False
        assert "minip" in reason


# ---------------------------------------------------------------------------
# Test: save() and load()
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_and_load_roundtrip(self, tmp_path):
        ws = ActiveWorkspace(
            active_group="quic",
            active_layers={"quic", "quic_tests"},
            active_tests=["/workspace/quic_tests/test_client.ivy"],
            granularity="test",
            set_by="explicit",
        )
        state_file = str(tmp_path / ".ivy-workspace-state.json")
        ws.save(state_file)

        loaded = ActiveWorkspace.load(state_file)
        assert loaded.active_group == "quic"
        assert loaded.active_layers == {"quic", "quic_tests"}
        assert loaded.active_tests == ["/workspace/quic_tests/test_client.ivy"]
        assert loaded.granularity == "test"
        assert loaded.set_by == "explicit"

    def test_save_creates_valid_json(self, tmp_path):
        ws = ActiveWorkspace(
            active_group="apt",
            active_layers={"apt"},
            active_tests=[],
            granularity="protocol",
            set_by="auto",
        )
        state_file = str(tmp_path / ".ivy-workspace-state.json")
        ws.save(state_file)

        with open(state_file) as f:
            data = json.load(f)

        assert data["version"] == 1
        assert data["active_group"] == "apt"
        assert set(data["active_layers"]) == {"apt"}
        assert data["active_tests"] == []
        assert data["granularity"] == "protocol"
        assert data["set_by"] == "auto"
        assert "timestamp" in data

    def test_load_missing_file_returns_cleared(self, tmp_path):
        state_file = str(tmp_path / "nonexistent.json")
        loaded = ActiveWorkspace.load(state_file)
        assert loaded.is_set() is False
        assert loaded.granularity == "none"

    def test_load_corrupt_file_returns_cleared(self, tmp_path):
        state_file = str(tmp_path / ".ivy-workspace-state.json")
        with open(state_file, "w") as f:
            f.write("NOT VALID JSON {{{{")

        loaded = ActiveWorkspace.load(state_file)
        assert loaded.is_set() is False
        assert loaded.granularity == "none"

    def test_load_empty_layers_preserves_cleared_semantics(self, tmp_path):
        """A saved state with granularity=none loads as cleared."""
        state_file = str(tmp_path / ".ivy-workspace-state.json")
        data = {
            "version": 1,
            "active_group": None,
            "active_layers": [],
            "active_tests": [],
            "granularity": "none",
            "set_by": "cleared",
            "timestamp": "2026-03-24T10:00:00+00:00",
        }
        with open(state_file, "w") as f:
            json.dump(data, f)

        loaded = ActiveWorkspace.load(state_file)
        assert loaded.is_set() is False


# ---------------------------------------------------------------------------
# Test: from_test_file()
# ---------------------------------------------------------------------------


class TestFromTestFile:
    def test_from_test_file_finds_group(
        self, simple_file_to_layer, workspace_groups, workspace_layers_list
    ):
        """Test file in a known group → workspace set to that group's layers."""
        ws = ActiveWorkspace.from_test_file(
            "/workspace/quic_tests/test_client.ivy",
            simple_file_to_layer,
            workspace_groups,
            workspace_layers=workspace_layers_list,
        )
        assert ws.is_set() is True
        assert ws.active_group == "quic"
        assert "quic" in ws.active_layers
        assert "quic_tests" in ws.active_layers
        assert ws.granularity == "test"

    def test_from_test_file_with_test_in_quic_layer(
        self, simple_file_to_layer, workspace_groups, workspace_layers_list
    ):
        """Test file in the base quic layer — group is still quic."""
        ws = ActiveWorkspace.from_test_file(
            "/workspace/quic/quic_types.ivy",
            simple_file_to_layer,
            workspace_groups,
            workspace_layers=workspace_layers_list,
        )
        assert ws.is_set() is True
        assert ws.active_group == "quic"

    def test_from_test_file_unknown_layer_falls_back(
        self, simple_file_to_layer, workspace_groups, workspace_layers_list
    ):
        """Layer not in any group → single-layer fallback."""
        # Add a file in a layer that isn't in workspace_groups
        file_to_layer = dict(simple_file_to_layer)
        file_to_layer["/workspace/extra/extra_file.ivy"] = "extra_layer"

        ws = ActiveWorkspace.from_test_file(
            "/workspace/extra/extra_file.ivy",
            file_to_layer,
            workspace_groups,
            workspace_layers=workspace_layers_list,
        )
        # Falls back to single layer
        assert ws.is_set() is True
        assert "extra_layer" in ws.active_layers
        assert ws.active_group is None

    def test_from_test_file_with_depends_on(
        self, simple_file_to_layer, workspace_layers_list
    ):
        """Fallback for unknown group includes depends_on layers from workspace_layers."""
        file_to_layer = dict(simple_file_to_layer)
        file_to_layer["/workspace/extra/extra_file.ivy"] = "quic_tests"

        # workspace_groups has no entry for quic_tests alone
        workspace_groups = {"other_group": ["apt"]}  # quic_tests not in any group

        ws = ActiveWorkspace.from_test_file(
            "/workspace/extra/extra_file.ivy",
            file_to_layer,
            workspace_groups,
            workspace_layers=workspace_layers_list,
        )
        # quic_tests depends_on quic, so quic should also be included
        assert ws.is_set() is True
        assert "quic_tests" in ws.active_layers
        assert "quic" in ws.active_layers

    def test_from_test_file_not_in_any_layer_returns_cleared(
        self, workspace_groups, workspace_layers_list
    ):
        """File not tracked in any layer → cleared() with warning."""
        ws = ActiveWorkspace.from_test_file(
            "/workspace/truly_unknown/file.ivy",
            {},  # empty file_to_layer
            workspace_groups,
            workspace_layers=workspace_layers_list,
        )
        assert ws.is_set() is False
        assert ws.granularity == "none"

    def test_from_test_file_no_workspace_layers_still_works(
        self, simple_file_to_layer, workspace_groups
    ):
        """from_test_file works even when workspace_layers is None."""
        ws = ActiveWorkspace.from_test_file(
            "/workspace/quic_tests/test_client.ivy",
            simple_file_to_layer,
            workspace_groups,
            workspace_layers=None,
        )
        # Should find the group and set workspace
        assert ws.is_set() is True
        assert ws.active_group == "quic"
