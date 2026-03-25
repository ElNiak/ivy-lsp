"""Tests for ActiveWorkspace integration into WorkspaceContext (Task 7).

Tests the RF-5 tiebreak logic in WorkspaceContext.load_active_workspace():
  - explicit persisted state ALWAYS wins
  - marker state is overridden by a new marker detection if protocol_id differs
  - no persisted state → cleared
"""

from __future__ import annotations

import json
import os

import pytest

from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
from ivy_lsp.core.workspace.context import WorkspaceContext
from ivy_lsp.core.workspace.detection import WorkspaceConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(tmp_path, state: dict) -> str:
    """Write a workspace state JSON file and return its path."""
    path = str(tmp_path / ".ivy-workspace-state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


def _explicit_state(active_group: str = "quic") -> dict:
    """Build a persisted state dict with set_by='explicit'."""
    return {
        "version": 1,
        "active_group": active_group,
        "active_layers": [active_group, f"{active_group}_tests"],
        "active_tests": [],
        "granularity": "protocol",
        "set_by": "explicit",
        "timestamp": "2026-03-24T10:00:00+00:00",
    }


def _marker_state(active_group: str = "quic") -> dict:
    """Build a persisted state dict with set_by='marker'."""
    return {
        "version": 1,
        "active_group": active_group,
        "active_layers": [active_group],
        "active_tests": [],
        "granularity": "protocol",
        "set_by": "marker",
        "timestamp": "2026-03-24T10:00:00+00:00",
    }


def _make_workspace_context(tmp_path) -> WorkspaceContext:
    """Create a minimal WorkspaceContext (without loading any index)."""
    ws_root = str(tmp_path)
    return WorkspaceContext(
        workspace_root=ws_root,
        project_type="fallback",
        workspace_config=WorkspaceConfig(
            workspace_root=ws_root,
            detected_by="fallback",
        ),
    )


# ---------------------------------------------------------------------------
# Test 1: Default active_workspace is cleared
# ---------------------------------------------------------------------------


class TestWorkspaceContextHasActiveWorkspace:
    """WorkspaceContext.__init__ always populates active_workspace as cleared."""

    def test_default_is_cleared(self, tmp_path):
        """A freshly constructed WorkspaceContext has active_workspace = cleared()."""
        ctx = _make_workspace_context(tmp_path)

        assert hasattr(ctx, "active_workspace")
        assert isinstance(ctx.active_workspace, ActiveWorkspace)
        assert ctx.active_workspace.is_set() is False
        assert ctx.active_workspace.set_by == "cleared"

    def test_active_workspace_attribute_exists_after_load(self, tmp_path, monkeypatch):
        """WorkspaceContext.load() produces a context with active_workspace set."""
        # Place a .ivyworkspace marker so detection doesn't wander
        marker = {
            "version": 3,
            "workspace_layers": [{"id": "default", "include_paths": []}],
        }
        (tmp_path / ".ivyworkspace").write_text(json.dumps(marker))
        monkeypatch.delenv("IVY_LSP_WORKSPACE", raising=False)
        monkeypatch.delenv("IVY_LSP_WORKSPACE_HINT", raising=False)

        ctx = WorkspaceContext.load(str(tmp_path))

        assert hasattr(ctx, "active_workspace")
        assert isinstance(ctx.active_workspace, ActiveWorkspace)


# ---------------------------------------------------------------------------
# Test 2: load_active_workspace — explicit persisted state wins
# ---------------------------------------------------------------------------


class TestLoadExplicitWinsOverMarker:
    """RF-5: explicit set_by always overrides any detected_protocol_id."""

    def test_explicit_wins_when_marker_detection_disagrees(self, tmp_path):
        """Explicit persisted state for 'quic' is kept even when detected='apt'."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _explicit_state("quic"))

        ctx.load_active_workspace(state_file, detected_protocol_id="apt")

        assert ctx.active_workspace.is_set() is True
        assert ctx.active_workspace.active_group == "quic"
        assert ctx.active_workspace.set_by == "explicit"

    def test_explicit_wins_with_no_detection(self, tmp_path):
        """Explicit persisted state is kept when detected_protocol_id=None."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _explicit_state("minip"))

        ctx.load_active_workspace(state_file, detected_protocol_id=None)

        assert ctx.active_workspace.active_group == "minip"
        assert ctx.active_workspace.set_by == "explicit"

    def test_explicit_wins_when_detection_matches(self, tmp_path):
        """Explicit state is kept even when detected_protocol_id agrees."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _explicit_state("quic"))

        ctx.load_active_workspace(state_file, detected_protocol_id="quic")

        assert ctx.active_workspace.active_group == "quic"
        assert ctx.active_workspace.set_by == "explicit"


# ---------------------------------------------------------------------------
# Test 3: load_active_workspace — marker state overridden by different protocol
# ---------------------------------------------------------------------------


class TestLoadMarkerOverriddenByDifferentProtocol:
    """RF-5: marker state is overridden when a new marker detects a different protocol_id."""

    def test_marker_state_overridden_when_new_protocol(self, tmp_path):
        """Persisted 'quic' marker is cleared when newly detected protocol is 'apt'."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _marker_state("quic"))

        ctx.load_active_workspace(state_file, detected_protocol_id="apt")

        # The old marker state should be replaced by cleared (new detection
        # will set it later via the workspace tool)
        assert ctx.active_workspace.is_set() is False

    def test_marker_state_kept_when_same_protocol(self, tmp_path):
        """Persisted 'quic' marker is kept when detected protocol is also 'quic'."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _marker_state("quic"))

        ctx.load_active_workspace(state_file, detected_protocol_id="quic")

        # Same protocol → keep the persisted marker state
        assert ctx.active_workspace.is_set() is True
        assert ctx.active_workspace.active_group == "quic"
        assert ctx.active_workspace.set_by == "marker"

    def test_marker_state_kept_when_no_detection(self, tmp_path):
        """Persisted marker state is kept when detected_protocol_id=None."""
        ctx = _make_workspace_context(tmp_path)
        state_file = _write_state(tmp_path, _marker_state("quic"))

        ctx.load_active_workspace(state_file, detected_protocol_id=None)

        assert ctx.active_workspace.is_set() is True
        assert ctx.active_workspace.active_group == "quic"


# ---------------------------------------------------------------------------
# Test 4: load_active_workspace — no state file stays cleared
# ---------------------------------------------------------------------------


class TestLoadNoStateFileStaysCleared:
    """Missing or corrupt state file → active_workspace remains cleared."""

    def test_missing_file_stays_cleared(self, tmp_path):
        """Non-existent state file → cleared workspace."""
        ctx = _make_workspace_context(tmp_path)
        missing_path = str(tmp_path / ".ivy-workspace-state.json")

        ctx.load_active_workspace(missing_path, detected_protocol_id="quic")

        assert ctx.active_workspace.is_set() is False
        assert ctx.active_workspace.set_by == "cleared"

    def test_missing_file_with_no_detection_stays_cleared(self, tmp_path):
        """Non-existent file + no detected_protocol_id → cleared workspace."""
        ctx = _make_workspace_context(tmp_path)
        missing_path = str(tmp_path / ".ivy-workspace-state.json")

        ctx.load_active_workspace(missing_path, detected_protocol_id=None)

        assert ctx.active_workspace.is_set() is False

    def test_corrupt_state_file_stays_cleared(self, tmp_path):
        """Corrupt JSON state file → cleared workspace (ActiveWorkspace.load fallback)."""
        ctx = _make_workspace_context(tmp_path)
        corrupt_path = str(tmp_path / ".ivy-workspace-state.json")
        with open(corrupt_path, "w") as f:
            f.write("NOT VALID JSON {{{")

        ctx.load_active_workspace(corrupt_path, detected_protocol_id="quic")

        assert ctx.active_workspace.is_set() is False
        assert ctx.active_workspace.set_by == "cleared"


# ---------------------------------------------------------------------------
# Test 5: load_active_workspace with detected_protocol_id but no persisted state
# ---------------------------------------------------------------------------


class TestLoadDetectedProtocolNoPersistedState:
    """No persisted state + detected_protocol_id → remains cleared (detection sets later)."""

    def test_no_state_and_detected_stays_cleared(self, tmp_path):
        """No file + detected_protocol_id → cleared (the workspace tool sets it later)."""
        ctx = _make_workspace_context(tmp_path)
        missing_path = str(tmp_path / ".ivy-workspace-state.json")

        ctx.load_active_workspace(missing_path, detected_protocol_id="quic")

        # No persisted state → cleared (workspace tool will activate later)
        assert ctx.active_workspace.is_set() is False
