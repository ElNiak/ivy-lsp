"""Three-tier analysis pipeline orchestrator.

Tier 1 (syntactic, <50ms): structural checks + RFC annotation parsing
Tier 2 (AST-enriched, <200ms): parser + type info + requirement extraction
Tier 3 (compiler, background): full compiler analysis (background thread)
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ivy_lsp.adapters.protocols import (
    IAstEnrichmentAdapter,
    ICompilerAdapter,
    IParserAdapter,
)
from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel
from ivy_lsp.semantic.nodes import SymbolNode, TypeNode
from ivy_lsp.semantic.rfc_annotations import parse_file_rfc_annotations

logger = logging.getLogger(__name__)


_T3_MAX_RESULTS = 100


@dataclass
class Tier3FileResult:
    """Per-file result of a Tier 3 compilation."""

    filepath: str
    success: bool
    started_at: float
    completed_at: float
    duration: float
    error_message: Optional[str] = None


@dataclass
class BulkAnalysisResult:
    """Result of a bulk T1+T2 analysis run."""

    total: int = 0
    t1_completed: int = 0
    t2_completed: int = 0
    errors: List[Tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


class AnalysisPipeline:
    """Orchestrates the three analysis tiers feeding into a SemanticModel."""

    def __init__(
        self,
        model: SemanticModel,
        parser_adapter: IParserAdapter,
        enrichment_adapter: IAstEnrichmentAdapter,
        compiler_adapter: ICompilerAdapter,
        compiler_manager: Any = None,
    ) -> None:
        self._model = model
        self._parser = parser_adapter
        self._enrichment = enrichment_adapter
        self._compiler = compiler_adapter
        self._compiler_manager = compiler_manager
        self._tier1_files: set[str] = set()
        self._tier2_files: set[str] = set()
        # Lock protecting all _tier3_* and _bulk_* state that is written
        # from background threads and read from the LSP main thread.
        self._state_lock = threading.Lock()
        self._tier3_results: OrderedDict[str, Tier3FileResult] = OrderedDict()
        self._tier3_running: bool = False
        self._tier3_current_file: Optional[str] = None
        self._tier3_last_file: Optional[str] = None
        self._tier3_last_completed_at: Optional[float] = None
        self._tier3_pending: int = 0
        self._bulk_running: bool = False
        self._bulk_total: int = 0
        self._bulk_completed: int = 0

    # -- Tier 1 ----------------------------------------------------------------

    def run_tier1(self, source: str, filepath: str) -> None:
        """Immediate syntactic analysis (<50ms, no ivy dep).

        - Parse RFC annotations from comments
        - Feed into model.update_file at tier1
        """
        nodes: List[Any] = []
        edges: List[Tuple[str, SemanticEdgeType, str]] = []

        # RFC annotation parsing from comments
        annotations = parse_file_rfc_annotations(source, filepath)
        for ann in annotations:
            nodes.append(ann)

        self._model.update_file(filepath, nodes, edges, "tier1")
        self._tier1_files.add(filepath)
        logger.debug("Tier 1 complete for %s: %d nodes", filepath, len(nodes))

    # -- Tier 2 ----------------------------------------------------------------

    def run_tier2(
        self, source: str, filepath: str, *, parse_result: Any = None
    ) -> Any:
        """AST-enriched analysis (<200ms, ivy parser).

        - Parse with parser_adapter (or reuse *parse_result* if provided)
        - Extract type info with enrichment_adapter
        - Build cross-reference edges (HAS_PARAM)
        - Re-parse RFC annotations to link with AST nodes
        - Feed into model.update_file at tier2

        Returns the ParseResult so callers can reuse it (avoiding double parse).
        """
        nodes: List[Any] = []
        edges: List[Tuple[str, SemanticEdgeType, str]] = []

        # Parse (reuse pre-parsed result if provided)
        if parse_result is not None and parse_result.success and parse_result.ast is not None:
            result = parse_result
        else:
            result = self._parser.parse(source, filepath)

        if not result.success or result.ast is None:
            logger.debug("Tier 2 parse failure for %s, RFC-only mode", filepath)
        if result.success and result.ast is not None:
            # Extract type info
            type_annotations = self._enrichment.extract_type_info(
                result.ast, filepath, source
            )
            for ta in type_annotations:
                if ta.is_enum or ta.sort_name != "action":
                    node: Any = TypeNode(
                        id=f"{filepath}:{ta.line}:{ta.name}",
                        name=ta.name,
                        qualified_name=ta.qualified_name,
                        file=filepath,
                        line=ta.line,
                        sort_name=ta.sort_name,
                        is_enum=ta.is_enum,
                        variants=ta.variants,
                        tier="tier2",
                    )
                else:
                    node = SymbolNode(
                        id=f"{filepath}:{ta.line}:{ta.name}",
                        name=ta.name,
                        qualified_name=ta.qualified_name,
                        kind="action",
                        file=filepath,
                        line=ta.line,
                        sort_name=ta.sort_name,
                        arity=ta.arity,
                        params=ta.params,
                        return_sort=ta.return_sort,
                        tier="tier2",
                    )
                nodes.append(node)

                # Wire HAS_PARAM edges for actions with parameters
                if hasattr(node, "params") and node.params:
                    for param in node.params:
                        # param format: "name:sort"
                        if ":" in param:
                            sort_part = param.split(":", 1)[1]
                            edges.append(
                                (node.id, SemanticEdgeType.HAS_PARAM, sort_part)
                            )

        # Also re-parse RFC annotations at tier2 to link them with AST nodes
        annotations = parse_file_rfc_annotations(source, filepath)
        for ann in annotations:
            nodes.append(ann)

        self._model.update_file(filepath, nodes, edges, "tier2")
        self._tier2_files.add(filepath)
        logger.debug(
            "Tier 2 complete for %s: %d nodes, %d edges",
            filepath,
            len(nodes),
            len(edges),
        )
        return result

    # -- Tier 3 ----------------------------------------------------------------

    def run_tier3_background(
        self, source: str, filepath: str, *, track_state: bool = True
    ) -> None:
        """Schedule Tier 3 compiler analysis in background thread.

        Args:
            track_state: When *False*, skip ``_tier3_running`` /
                ``_tier3_current_file`` flag updates.  Used by the deep
                indexer to submit many files without corrupting the
                monitoring display.
        """
        from ivy_lsp.adapters.protocols import CompileResult

        t3_start = time.monotonic()

        def _on_result(result: CompileResult) -> None:
            t3_end = time.monotonic()
            duration = t3_end - t3_start
            completed_at = time.time()

            if not result.success:
                error_msgs = [e.message for e in result.errors]
                logger.debug(
                    "Tier 3 compilation failed for %s: %s",
                    filepath,
                    error_msgs,
                )
                self._record_tier3_result(
                    filepath,
                    success=False,
                    started_at=completed_at - duration,
                    completed_at=completed_at,
                    duration=duration,
                    error_message="; ".join(error_msgs) if error_msgs else "Unknown error",
                )
                if not track_state:
                    with self._state_lock:
                        self._tier3_pending = max(0, self._tier3_pending - 1)
                if track_state:
                    with self._state_lock:
                        self._tier3_running = False
                        self._tier3_current_file = None
                return
            nodes: List[Any] = []
            edges: List[Tuple[str, SemanticEdgeType, str]] = []

            # Enrich semantic model from compiled data if available
            if self._compiler_manager is not None:
                try:
                    ir = self._compiler_manager.get_cached(filepath)
                    if ir is not None:
                        from ivy_lsp.compilation.graph_enrichment import (
                            enrich_semantic_model,
                        )
                        enrich_semantic_model(self._model, ir, filepath)
                except Exception:
                    logger.debug("Tier 3 enrichment failed", exc_info=True)

            self._model.update_file(filepath, nodes, edges, "tier3")
            self._record_tier3_result(
                filepath,
                success=True,
                started_at=completed_at - duration,
                completed_at=completed_at,
                duration=duration,
            )
            if not track_state:
                with self._state_lock:
                    self._tier3_pending = max(0, self._tier3_pending - 1)
            if track_state:
                with self._state_lock:
                    self._tier3_running = False
                    self._tier3_current_file = None
            logger.debug("Tier 3 complete for %s (%.2fs)", filepath, duration)

        if not track_state:
            with self._state_lock:
                self._tier3_pending += 1
        if track_state:
            with self._state_lock:
                self._tier3_running = True
                self._tier3_current_file = filepath
        try:
            if hasattr(self._compiler, "compile_background"):
                self._compiler.compile_background(source, filepath, _on_result)
            else:
                # Synchronous fallback
                result = self._compiler.compile(source, filepath)
                _on_result(result)
        except Exception:
            if not track_state:
                with self._state_lock:
                    self._tier3_pending = max(0, self._tier3_pending - 1)
            if track_state:
                with self._state_lock:
                    self._tier3_running = False
                    self._tier3_current_file = None
            raise

    # -- Bulk T1+T2 analysis ---------------------------------------------------

    def run_bulk_t1_t2(
        self,
        filepaths: List[str],
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        include_t2: bool = True,
    ) -> BulkAnalysisResult:
        """Run T1 (and optionally T2) analysis on a batch of files.

        Designed for background pre-population of the semantic model after
        deep indexing completes.  Each file is processed independently;
        errors are recorded but never crash the batch.

        Args:
            filepaths: Absolute paths to ``.ivy`` files.
            progress_callback: Called after each file with (completed, total, current_file).
            cancel_event: If set, the batch aborts early.
            include_t2: When *False*, only T1 runs (~5x faster).

        Returns:
            A :class:`BulkAnalysisResult` summarising outcomes.
        """
        # Filter out files already analysed at the requested tier
        if include_t2:
            remaining = [f for f in filepaths if f not in self._tier2_files]
        else:
            remaining = [f for f in filepaths if f not in self._tier1_files]

        result = BulkAnalysisResult(total=len(remaining))
        with self._state_lock:
            self._bulk_running = True
            self._bulk_total = len(remaining)
            self._bulk_completed = 0

        try:
            for i, filepath in enumerate(remaining):
                if cancel_event is not None and cancel_event.is_set():
                    result.cancelled = True
                    break

                try:
                    with open(filepath) as f:
                        source = f.read()
                except OSError as exc:
                    result.errors.append((filepath, str(exc)))
                    with self._state_lock:
                        self._bulk_completed = i + 1
                    if progress_callback is not None:
                        try:
                            progress_callback(i + 1, len(remaining), filepath)
                        except Exception:
                            pass
                    continue

                try:
                    self.run_tier1(source, filepath)
                    result.t1_completed += 1
                except Exception as exc:
                    result.errors.append((filepath, f"T1: {exc}"))
                    with self._state_lock:
                        self._bulk_completed = i + 1
                    if progress_callback is not None:
                        try:
                            progress_callback(i + 1, len(remaining), filepath)
                        except Exception:
                            pass
                    continue

                if include_t2:
                    try:
                        self.run_tier2(source, filepath)
                        result.t2_completed += 1
                    except Exception as exc:
                        result.errors.append((filepath, f"T2: {exc}"))

                with self._state_lock:
                    self._bulk_completed = i + 1
                if progress_callback is not None:
                    try:
                        progress_callback(i + 1, len(remaining), filepath)
                    except Exception:
                        pass
        finally:
            with self._state_lock:
                self._bulk_running = False

        logger.info(
            "Bulk analysis: %d/%d T1, %d/%d T2, %d errors, cancelled=%s",
            result.t1_completed,
            result.total,
            result.t2_completed,
            result.total,
            len(result.errors),
            result.cancelled,
        )
        return result

    # -- T3 result management --------------------------------------------------

    def _record_tier3_result(
        self,
        filepath: str,
        *,
        success: bool,
        started_at: float,
        completed_at: float,
        duration: float,
        error_message: Optional[str] = None,
    ) -> None:
        """Store a Tier 3 file result, evicting oldest if over capacity.

        Called from background threads -- acquires ``_state_lock``.
        """
        with self._state_lock:
            self._tier3_results[filepath] = Tier3FileResult(
                filepath=filepath,
                success=success,
                started_at=started_at,
                completed_at=completed_at,
                duration=duration,
                error_message=error_message,
            )
            self._tier3_last_file = filepath
            self._tier3_last_completed_at = completed_at
            while len(self._tier3_results) > _T3_MAX_RESULTS:
                self._tier3_results.popitem(last=False)

    def get_tier3_file_results(self) -> List[Dict[str, Any]]:
        """Return per-file T3 results sorted by completion time (newest first).

        Acquires ``_state_lock`` to snapshot results safely.
        """
        with self._state_lock:
            snapshot = list(reversed(self._tier3_results.values()))
        return [
            {
                "file": r.filepath,
                "success": r.success,
                "duration": round(r.duration, 2),
                "error": r.error_message,
            }
            for r in snapshot
        ]

    # -- State query -----------------------------------------------------------

    def get_pipeline_state(self) -> dict:
        """Return current pipeline state for monitoring.

        Acquires ``_state_lock`` to get a consistent snapshot of T3 and
        bulk analysis state.
        """
        with self._state_lock:
            succeeded = sum(1 for r in self._tier3_results.values() if r.success)
            failed = sum(1 for r in self._tier3_results.values() if not r.success)
            t3_count = len(self._tier3_results)
            t3_running = self._tier3_running
            t3_current = self._tier3_current_file
            t3_last = self._tier3_last_file
            t3_last_at = self._tier3_last_completed_at
            t3_pending = self._tier3_pending
            bulk_running = self._bulk_running
            bulk_total = self._bulk_total
            bulk_completed = self._bulk_completed
        return {
            "tier1FileCount": len(self._tier1_files),
            "tier2FileCount": len(self._tier2_files),
            "tier3FileCount": t3_count,
            "tier3Running": t3_running,
            "tier3Succeeded": succeeded,
            "tier3Failed": failed,
            "tier3CurrentFile": t3_current,
            "tier3LastFile": t3_last,
            "tier3LastCompletedAt": t3_last_at,
            "tier3Pending": t3_pending,
            "semanticNodeCount": self._model.node_count(),
            "semanticEdgeCount": self._model.edge_count(),
            "semanticModelReady": self._model.node_count() > 0,
            "bulkAnalysisRunning": bulk_running,
            "bulkAnalysisTotal": bulk_total,
            "bulkAnalysisCompleted": bulk_completed,
        }

    # -- Orchestration ---------------------------------------------------------

    def analyze(self, source: str, filepath: str, trigger: str = "change") -> Any:
        """Run appropriate tiers based on trigger.

        trigger: "change" -> T1+T2, "save" -> T1+T2+T3, "command" -> T3 only

        Returns the ParseResult from Tier 2 (or None if Tier 2 was not run)
        so that callers can reuse it and avoid a redundant parse.
        """
        parse_result = None
        if trigger in ("change", "save"):
            self.run_tier1(source, filepath)
            parse_result = self.run_tier2(source, filepath)
        if trigger in ("save", "command"):
            self.run_tier3_background(source, filepath)
        return parse_result
