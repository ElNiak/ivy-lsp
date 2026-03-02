"""Typed protocols for server interface contracts."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ivy_lsp.analysis.requirement_graph import RequirementGraph
    from ivy_lsp.compilation.compiler_manager import CompilerManager
    from ivy_lsp.features.status import ServerStateTracker
    from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline


@runtime_checkable
class IvyServerProtocol(Protocol):
    """Minimal server interface expected by feature handlers.

    Describes the attributes and methods of IvyLanguageServer that are accessed
    by LSP feature handlers (commands, monitoring, visualization, etc.).
    """

    _indexer: Optional[WorkspaceIndexer]
    _full_mode: bool
    _initializing: bool
    _semantic_model: Any
    _analysis_pipeline: Optional[AnalysisPipeline]
    _compiler_manager: Optional[CompilerManager]
    _parser: Any
    state_tracker: ServerStateTracker
    workspace: Any
    work_done_progress: Any
    protocol: Any


@runtime_checkable
class IndexerLike(Protocol):
    """Minimal indexer interface for visualization proxy."""

    _requirement_graph: Optional[RequirementGraph]
