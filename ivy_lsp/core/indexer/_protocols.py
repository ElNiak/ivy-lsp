"""Typing protocol for WorkspaceIndexer host class.

Declares attributes accessed by DeepIndexMixin and ScopeManagerMixin
so pyright can verify mixin attribute access without circular imports.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol, Union

if TYPE_CHECKING:
    from ivy_lsp.core.analysis.mirror import MirrorRegistry
    from ivy_lsp.core.analysis.test_scope import (
        ExportImportInfo,
        ScopedRequirementModel,
    )
    from ivy_lsp.core.indexer.file_cache import FileCache, PersistentFileCache
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import DeepIndexProgress
    from ivy_lsp.core.parsing.symbols import IncludeGraph, IvySymbol, SymbolTable


class WorkspaceIndexerHost(Protocol):
    """Protocol declaring attributes available on the WorkspaceIndexer host."""

    # -- Threading primitives --
    _progress_lock: threading.Lock
    _table_lock: threading.Lock
    _exports_lock: threading.Lock
    _stop_requested: threading.Event
    _source_cache_lock: threading.Lock

    # -- Data stores --
    _symbol_table: "SymbolTable"
    _cache: "Union[FileCache, PersistentFileCache]"
    _requirement_graph: "ScopedRequirementModel"
    _include_graph: "IncludeGraph"
    _file_export_imports: Dict[str, "ExportImportInfo"]
    _file_export_hashes: Dict[str, str]
    _mirror_scope_cache: Dict[str, List["IvySymbol"]]
    _mirror_registry: "MirrorRegistry"
    _index_errors: List[Dict[str, str]]
    _source_cache: Dict[str, tuple]

    # -- Progress and lifecycle --
    _progress_callback: Optional[Callable[[int, int, Optional[str]], None]]
    _done_callback: Optional[Callable[[], None]]
    _deep_index_progress: "DeepIndexProgress"
    _deep_index_running: bool

    # -- Sub-objects --
    _parser: Any
    _resolver: "IncludeResolver"
    _analysis_pipeline: Optional[Any]
    _workspace_root: str

    # -- Methods (defined on WorkspaceIndexer or other mixins) --
    def _read_source(self, filepath: str) -> Optional[str]: ...
    def _clear_source_cache(self) -> None: ...
    def _wire_requirement_graph(self) -> None: ...
    def _compute_test_scopes(self, dirty_files: Optional[set] = None) -> None: ...
    def _extract_file_requirements(
        self, filepath: str, result: Any, source: str
    ) -> None: ...
    def _extract_file_exports_imports(
        self, filepath: str, result: Any, source: str
    ) -> None: ...
    def _extract_includes(self, source: str) -> List[str]: ...
    def _index_single_file(self, filepath: str) -> List["IvySymbol"]: ...
    def _remove_file_symbols(self, filepath: str) -> None: ...
