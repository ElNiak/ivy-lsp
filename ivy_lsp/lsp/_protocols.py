"""Typing protocol for IvyLanguageServer host class.

Declares private attributes accessed by BulkOrchestrationMixin and
ServerSetupMixin.  LanguageServer-provided attributes (protocol,
workspace, etc.) are inherited via the combined base class in each
mixin's TYPE_CHECKING block.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol

if TYPE_CHECKING:
    from ivy_lsp.core.compilation.compiler_manager import CompilerManager
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.semantic.analysis_pipeline import AnalysisPipeline
    from ivy_lsp.core.semantic.model import SemanticModel
    from ivy_lsp.lsp.ui.status import ServerStateTracker


class IvyServerHost(Protocol):
    """Protocol declaring private attributes on IvyLanguageServer.

    LanguageServer-provided members (protocol, workspace,
    window_show_message, etc.) are NOT declared here to avoid
    conflicts when IvyLanguageServer also inherits LanguageServer.
    """

    _indexer: "Optional[WorkspaceIndexer]"
    _parser: Optional[Any]
    _full_mode: bool
    _semantic_model: "Optional[SemanticModel]"
    _analysis_pipeline: "Optional[AnalysisPipeline]"
    _bulk_analysis_cancel: threading.Event
    _shutdown_event: threading.Event
    state_tracker: "ServerStateTracker"
    _compiler_manager: "Optional[CompilerManager]"
    _client_supports_work_done_progress: bool

    def _make_progress_callback(
        self,
        title: str,
        begin_msg: str,
        end_msg: str,
        throttle_seconds: float = ...,
    ) -> Callable[[int, int, Optional[str]], None]: ...
    def _start_bulk_analysis(self) -> None: ...
    def _send_compilation_progress(
        self, completed: int, total: int, filepath: str, success: bool
    ) -> None: ...
    def _send_model_ready_notification(self) -> None: ...
