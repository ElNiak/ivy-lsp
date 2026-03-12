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
    from ivy_lsp.analysis.requirement_graph import EdgeType, RequirementGraph
    from ivy_lsp.compilation.compiler_manager import CompilerManager
    from ivy_lsp.features.status import ServerStateTracker
    from ivy_lsp.indexer.include_resolver import IncludeResolver
    from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.parsing.symbols import IncludeGraph, IvySymbol
    from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline
    from ivy_lsp.semantic.model import SemanticModel


@runtime_checkable
class IParserAdapter(Protocol):
    """Interface for Ivy parser adapters (full or fallback)."""

    def parse(self, source: str, filename: str) -> Any: ...


@runtime_checkable
class IvyServerProtocol(Protocol):
    """Minimal server interface expected by feature handlers.

    Describes the public attributes and methods of IvyLanguageServer that
    are accessed by LSP feature handlers (commands, monitoring, visualization, etc.).
    """

    @property
    def indexer(self) -> Optional[WorkspaceIndexer]: ...
    @property
    def full_mode(self) -> bool: ...
    @property
    def initializing(self) -> bool: ...
    @property
    def semantic_model(self) -> Optional[SemanticModel]: ...
    @property
    def analysis_pipeline(self) -> Optional[AnalysisPipeline]: ...
    @property
    def compiler_manager(self) -> Optional[CompilerManager]: ...
    @property
    def parser(self) -> Optional[IParserAdapter]: ...
    @property
    def bulk_analysis_cancel(self) -> threading.Event: ...

    state_tracker: ServerStateTracker
    workspace: Any
    work_done_progress: Any
    protocol: Any


@runtime_checkable
class IIndexer(Protocol):
    """Public interface for the workspace indexer."""

    @property
    def requirement_graph(self) -> Optional[RequirementGraph]: ...
    @property
    def include_graph(self) -> IncludeGraph: ...
    @property
    def resolver(self) -> IncludeResolver: ...

    def lookup_all_symbols(self) -> List[IvySymbol]: ...
    def lookup_qualified_symbols(self, name: str) -> List[IvySymbol]: ...
    def get_deep_index_progress(self) -> Dict[str, Any]: ...
    def get_file_export_imports(self) -> Dict[str, Any]: ...
    def get_cached_file(self, filepath: str) -> Any: ...


@runtime_checkable
class IRequirementGraph(Protocol):
    """Public interface for the requirement graph."""

    def get_outgoing_edges(self, node_id: str) -> List[Tuple[EdgeType, str]]: ...
