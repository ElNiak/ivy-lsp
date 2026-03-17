"""Typed protocols for server interface contracts."""

from __future__ import annotations

import threading
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

if TYPE_CHECKING:
    from ivy_lsp.adapters.protocols import IParserAdapter
    from ivy_lsp.analysis.requirement_graph import EdgeType, RequirementGraph
    from ivy_lsp.compilation.compiler_manager import CompilerManager
    from ivy_lsp.features.status import ServerStateTracker
    from ivy_lsp.indexer.include_resolver import IncludeResolver
    from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.parsing.symbols import IncludeGraph, IvySymbol
    from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline
    from ivy_lsp.semantic.model import SemanticModel


@runtime_checkable
class IvyServerProtocol(Protocol):
    """Minimal server interface expected by feature handlers.

    Describes the public attributes and methods of IvyLanguageServer that
    are accessed by LSP feature handlers (commands, monitoring, visualization, etc.).
    """

    @property
    def indexer(self) -> Optional[WorkspaceIndexer]:
        """Return the workspace indexer, if initialized."""
        ...

    @property
    def full_mode(self) -> bool:
        """Return whether full (Ivy-backed) mode is active."""
        ...

    @property
    def initializing(self) -> bool:
        """Return whether the server is still initializing."""
        ...

    @property
    def semantic_model(self) -> Optional[SemanticModel]:
        """Return the semantic model, if available."""
        ...

    @property
    def analysis_pipeline(self) -> Optional[AnalysisPipeline]:
        """Return the analysis pipeline, if available."""
        ...

    @property
    def compiler_manager(self) -> Optional[CompilerManager]:
        """Return the compiler manager, if available."""
        ...

    @property
    def parser(self) -> Optional[IParserAdapter]:
        """Return the parser adapter, if available."""
        ...

    @property
    def bulk_analysis_cancel(self) -> threading.Event:
        """Return the cancellation event for bulk analysis."""
        ...

    _client_supports_work_done_progress: bool
    state_tracker: ServerStateTracker
    workspace: Any
    work_done_progress: Any
    protocol: Any


@runtime_checkable
class IIndexer(Protocol):
    """Public interface for the workspace indexer."""

    @property
    def requirement_graph(self) -> Optional[RequirementGraph]:
        """Return the requirement graph, if built."""
        ...

    @property
    def include_graph(self) -> IncludeGraph:
        """Return the include dependency graph."""
        ...

    @property
    def resolver(self) -> IncludeResolver:
        """Return the include path resolver."""
        ...

    def lookup_all_symbols(self) -> List[IvySymbol]:
        """Return all symbols across the workspace."""
        ...

    def lookup_qualified_symbols(self, name: str) -> List[IvySymbol]:
        """Return symbols matching a qualified name."""
        ...

    def get_deep_index_progress(self) -> Dict[str, Any]:
        """Return progress info for the deep indexing pass."""
        ...

    def get_file_export_imports(self) -> Dict[str, Any]:
        """Return export/import info for all indexed files."""
        ...

    def get_cached_file(self, filepath: str) -> Any:
        """Return the cached parse result for a file."""
        ...


@runtime_checkable
class IRequirementGraph(Protocol):
    """Public interface for the requirement graph."""

    def get_outgoing_edges(self, node_id: str) -> List[Tuple[EdgeType, str]]:
        """Return outgoing edges for the given node."""
        ...
