"""Workspace management: detection, context, overlays, and active workspace scoping."""

from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
from ivy_lsp.core.workspace.context import (
    ProtocolIndex,
    StalenessInfo,
    WorkspaceContext,
)
from ivy_lsp.core.workspace.detection import (
    WorkspaceConfig,
    WorkspaceLayer,
    detect_ivy_workspace,
)
from ivy_lsp.core.workspace.session_overlay import (
    OverlayEntry,
    SessionOverlay,
    TestScopeView,
)

__all__ = [
    "ActiveWorkspace",
    "WorkspaceContext",
    "ProtocolIndex",
    "StalenessInfo",
    "WorkspaceConfig",
    "WorkspaceLayer",
    "detect_ivy_workspace",
    "SessionOverlay",
    "TestScopeView",
    "OverlayEntry",
]
