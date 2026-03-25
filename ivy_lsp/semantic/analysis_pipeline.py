"""Three-tier analysis pipeline orchestrator.

Tier 1 (syntactic, target <50ms): structural checks + RFC annotation parsing
Tier 2 (AST-enriched, target <200ms): parser + type info + requirement extraction
Tier 3 (compiler, background): full compiler analysis (background thread)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ivy_lsp.adapters.protocols import (
    IAstEnrichmentAdapter,
    ICompilerAdapter,
    IParserAdapter,
)
from ivy_lsp.config import get_config
from ivy_lsp.observability import LogCategory, log_phase, timed_phase
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


@dataclass
class _TierState:
    """Consolidated state for a single analysis tier."""

    running: bool = False
    total: int = 0
    completed: int = 0
    cancelled: bool = False
    current_file: Optional[str] = None
    last_file: Optional[str] = None
    last_completed_at: Optional[float] = None
    pending: int = 0


class AnalysisPipeline:
    """Orchestrates the three analysis tiers feeding into a SemanticModel."""

    def __init__(
        self,
        model: SemanticModel,
        parser_adapter: IParserAdapter,
        enrichment_adapter: IAstEnrichmentAdapter,
        compiler_adapter: ICompilerAdapter,
        compiler_manager: Any = None,
        test_file_resolver: Optional[Callable[[str], Optional[str]]] = None,
        requirement_graph: Any = None,
        notification_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """Initialize pipeline with adapters and empty tier state."""
        self._model = model
        self._parser = parser_adapter
        self._enrichment = enrichment_adapter
        self._compiler = compiler_adapter
        self._compiler_manager = compiler_manager
        self._test_file_resolver = test_file_resolver
        self._requirement_graph = requirement_graph
        self._notification_callback = notification_callback
        self._tier1_files: set[str] = set()
        self._tier2_files: set[str] = set()
        # Lock protecting _tier1_files, _tier2_files, and the _TierState
        # instances (_tier3, _bulk, _bulk_compile) that are written from
        # background threads and read from the LSP main thread.
        self._state_lock = threading.Lock()
        self._tier3_results: OrderedDict[str, Tier3FileResult] = OrderedDict()
        self._tier3 = _TierState()
        self._bulk = _TierState()
        # Bulk compilation state (workspace-wide T3)
        self._bulk_compile = _TierState()
        self._file_generation: Dict[str, int] = {}

    # -- Convenience factory ---------------------------------------------------

    @staticmethod
    def build_model_from_files(
        root: str,
        find_files_fn: Callable[[str], List[str]],
        include_resolver: Any = None,
        stdlib_modules: Optional[frozenset] = None,
    ) -> Optional[SemanticModel]:
        """Build a pre-populated SemanticModel from workspace files.

        Delegates to :func:`ivy_lsp.semantic.model_builder.build_semantic_model`,
        providing a single entry point shared with the MCP server's
        standalone model builder.

        This is useful for pre-populating a model before constructing an
        ``AnalysisPipeline``, or for batch re-indexing.

        Parameters
        ----------
        root:
            Workspace root directory.
        find_files_fn:
            Callable returning relative ``.ivy`` file paths.
        include_resolver:
            Optional resolve callback for parser include resolution.
        stdlib_modules:
            Known Ivy stdlib module names (forwarded to the builder).

        Returns:
            SemanticModel or ``None`` when dependencies are missing.
        """
        from ivy_lsp.semantic.model_builder import build_semantic_model

        return build_semantic_model(
            root=root,
            find_files_fn=find_files_fn,
            include_resolver=include_resolver,
            stdlib_modules=stdlib_modules,
        )

    # -- Tier 1 ----------------------------------------------------------------

    def run_tier1(self, source: str, filepath: str) -> List[Any]:
        """Immediate syntactic analysis (<50ms, no ivy dep).

        - Parse RFC annotations from comments
        - Feed into model.update_file at tier1

        Returns the parsed RFC annotation nodes so callers can pass them
        to run_tier2 via the *rfc_annotations* kwarg, avoiding a redundant
        re-parse of the same source text.
        """
        with timed_phase(
            logger,
            category=LogCategory.PERFORMANCE,
            phase="tier1",
            name="analysis_tier1",
            channel="analysis",
            payload={"filepath": filepath},
        ):
            edges: List[Tuple[str, SemanticEdgeType, str]] = []

            # RFC annotation parsing from comments
            annotations = parse_file_rfc_annotations(source, filepath)

            self._model.update_file(filepath, list(annotations), edges, "tier1")
        with self._state_lock:
            self._file_generation[filepath] = self._file_generation.get(filepath, 0) + 1
            self._tier1_files.add(filepath)
        log_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="tier1",
            message="Tier 1 complete",
            data={"filepath": filepath, "annotations": len(annotations)},
            level=logging.INFO,
        )
        logger.debug("Tier 1 complete for %s: %d nodes", filepath, len(annotations))
        return annotations

    # -- Tier 2 ----------------------------------------------------------------

    def run_tier2(
        self,
        source: str,
        filepath: str,
        *,
        parse_result: Any = None,
        rfc_annotations: Optional[List[Any]] = None,
    ) -> Any:
        """AST-enriched analysis (<200ms, ivy parser).

        - Parse with parser_adapter (or reuse *parse_result* if provided)
        - Extract type info with enrichment_adapter
        - Build cross-reference edges (HAS_PARAM)
        - Include RFC annotations (reused from T1 via *rfc_annotations*, or
          re-parsed if not provided)
        - Feed into model.update_file at tier2

        Returns the ParseResult so callers can reuse it (avoiding double parse).
        """
        with timed_phase(
            logger,
            category=LogCategory.PERFORMANCE,
            phase="tier2",
            name="analysis_tier2",
            channel="analysis",
            payload={"filepath": filepath},
        ):
            nodes: List[Any] = []
            edges: List[Tuple[str, SemanticEdgeType, str]] = []

            # Parse (reuse pre-parsed result if provided)
            if (
                parse_result is not None
                and parse_result.success
                and parse_result.ast is not None
            ):
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

            # Reuse RFC annotations from T1 when provided; otherwise re-parse.
            if rfc_annotations is not None:
                annotations = rfc_annotations
            else:
                annotations = parse_file_rfc_annotations(source, filepath)
            for ann in annotations:
                nodes.append(ann)

            self._model.update_file(filepath, nodes, edges, "tier2")
        with self._state_lock:
            self._tier2_files.add(filepath)
        log_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="tier2",
            message="Tier 2 complete",
            data={"filepath": filepath, "nodes": len(nodes), "edges": len(edges)},
            level=logging.INFO,
        )
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
            source: The Ivy source text to analyze.
            filepath: Absolute path to the source file.
            track_state: When *False*, skip ``_tier3.running`` /
                ``_tier3.current_file`` flag updates.  Used by the deep
                indexer to submit many files without corrupting the
                monitoring display.
        """
        from ivy_lsp.adapters.protocols import CompileResult

        t3_start = time.time()
        with self._state_lock:
            gen_at_submit = self._file_generation.get(filepath, 0)

        def _on_result(result: CompileResult) -> None:
            t3_end = time.time()
            duration = t3_end - t3_start
            completed_at = t3_end

            try:
                with self._state_lock:
                    current_gen = self._file_generation.get(filepath, 0)
                if current_gen != gen_at_submit:
                    logger.debug(
                        "Discarding stale T3 result for %s (gen %d vs %d)",
                        filepath,
                        gen_at_submit,
                        current_gen,
                    )
                    return
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
                        started_at=t3_start,
                        completed_at=completed_at,
                        duration=duration,
                        error_message=(
                            "; ".join(error_msgs) if error_msgs else "Unknown error"
                        ),
                    )
                    return
                enrichment_performed = False

                # Enrich semantic model and requirement graph from compiled data
                if self._compiler_manager is not None:
                    try:
                        ir = self._compiler_manager.get_cached(filepath)
                        if ir is not None:
                            from ivy_lsp.compilation.graph_enrichment import (
                                enrich_requirement_graph,
                                enrich_semantic_model,
                            )

                            enrich_semantic_model(self._model, ir, filepath)
                            enrichment_performed = True
                            if self._requirement_graph is not None:
                                enrich_requirement_graph(self._requirement_graph, ir)
                    except Exception:
                        logger.warning(
                            "Tier 3 enrichment failed for %s", filepath, exc_info=True
                        )

                # Only mark file as tier3-processed if enrichment didn't already
                # call model.update_file (graph_enrichment.py does it internally)
                if not enrichment_performed:
                    self._model.update_file(filepath, [], [], "tier3")
                self._record_tier3_result(
                    filepath,
                    success=True,
                    started_at=t3_start,
                    completed_at=completed_at,
                    duration=duration,
                )
                logger.debug("Tier 3 complete for %s (%.2fs)", filepath, duration)
            finally:
                if not track_state:
                    with self._state_lock:
                        self._tier3.pending = max(0, self._tier3.pending - 1)
                if track_state:
                    with self._state_lock:
                        self._tier3.running = False
                        self._tier3.current_file = None

        if not track_state:
            with self._state_lock:
                self._tier3.pending += 1
        if track_state:
            with self._state_lock:
                self._tier3.running = True
                self._tier3.current_file = filepath

        _on_result_called = False  # Guard against double-decrement in sync path

        try:
            if hasattr(self._compiler, "compile_background"):
                self._compiler.compile_background(source, filepath, _on_result)
            else:
                # Synchronous fallback
                result = self._compiler.compile(source, filepath)
                _on_result_called = True
                _on_result(result)
        except Exception:
            if not _on_result_called:
                # Only decrement if _on_result's finally hasn't already done it
                if not track_state:
                    with self._state_lock:
                        self._tier3.pending = max(0, self._tier3.pending - 1)
                if track_state:
                    with self._state_lock:
                        self._tier3.running = False
                        self._tier3.current_file = None
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

        When ``IVY_LSP_BULK_WORKERS`` > 1 (default 4), T1 analysis runs
        in a thread pool for ~2-4x speedup.  T2 remains sequential
        because the Ivy parser may have global state.

        Args:
            filepaths: Absolute paths to ``.ivy`` files.
            progress_callback: Called after each file with (completed, total, current_file).
            cancel_event: If set, the batch aborts early.
            include_t2: When *False*, only T1 runs (~5x faster).

        Returns:
            A :class:`BulkAnalysisResult` summarising outcomes.
        """
        with timed_phase(
            logger,
            category=LogCategory.PERFORMANCE,
            phase="bulk",
            name="run_bulk_t1_t2",
            channel="analysis",
            payload={"files": len(filepaths), "include_t2": include_t2},
        ):
            with self._state_lock:
                if include_t2:
                    remaining = [f for f in filepaths if f not in self._tier2_files]
                else:
                    remaining = [f for f in filepaths if f not in self._tier1_files]
                self._bulk.running = True
                self._bulk.total = len(remaining)
                self._bulk.completed = 0

            result = BulkAnalysisResult(total=len(remaining))

            num_workers = get_config().bulk_workers
            try:
                if num_workers > 1 and len(remaining) > 3:
                    self._run_bulk_parallel_t1(
                        remaining,
                        result,
                        progress_callback,
                        cancel_event,
                        include_t2,
                    )
                else:
                    self._run_bulk_sequential(
                        remaining,
                        result,
                        progress_callback,
                        cancel_event,
                        include_t2,
                    )
            finally:
                with self._state_lock:
                    self._bulk.running = False

        log_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="bulk",
            message="Bulk T1/T2 analysis complete",
            data={
                "total": result.total,
                "t1_completed": result.t1_completed,
                "t2_completed": result.t2_completed,
                "errors": len(result.errors),
                "cancelled": result.cancelled,
            },
            level=logging.INFO,
        )
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

    def _bulk_progress_tick(
        self,
        idx: int,
        total: int,
        filepath: str,
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]],
    ) -> None:
        """Update bulk progress counter and invoke callback."""
        with self._state_lock:
            self._bulk.completed = idx + 1
        if progress_callback is not None:
            try:
                progress_callback(idx + 1, total, filepath)
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)

    def _run_bulk_sequential(
        self,
        remaining: List[str],
        result: BulkAnalysisResult,
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]],
        cancel_event: Optional[threading.Event],
        include_t2: bool,
    ) -> None:
        """Sequential bulk T1+T2 with parse-result reuse."""
        for i, filepath in enumerate(remaining):
            if cancel_event is not None and cancel_event.is_set():
                result.cancelled = True
                break

            try:
                with open(filepath) as f:
                    source = f.read()
            except OSError as exc:
                result.errors.append((filepath, str(exc)))
                self._bulk_progress_tick(i, len(remaining), filepath, progress_callback)
                continue

            t1_annotations = None
            try:
                t1_annotations = self.run_tier1(source, filepath)
                result.t1_completed += 1
            except Exception as exc:
                result.errors.append((filepath, f"T1: {exc}"))
                self._bulk_progress_tick(i, len(remaining), filepath, progress_callback)
                continue

            if include_t2:
                parse_result = None
                try:
                    parse_result = self._parser.parse(source, filepath)
                except Exception as exc:
                    logger.debug("Bulk T2 parse failed for %s: %s", filepath, exc)
                    result.errors.append((filepath, f"T2-parse: {exc}"))
                try:
                    self.run_tier2(
                        source,
                        filepath,
                        parse_result=parse_result,
                        rfc_annotations=t1_annotations,
                    )
                    result.t2_completed += 1
                except Exception as exc:
                    result.errors.append((filepath, f"T2: {exc}"))

            self._bulk_progress_tick(i, len(remaining), filepath, progress_callback)

    def _run_bulk_parallel_t1(
        self,
        remaining: List[str],
        result: BulkAnalysisResult,
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]],
        cancel_event: Optional[threading.Event],
        include_t2: bool,
    ) -> None:
        """Parallel T1 + sequential T2.

        T1 (regex-based) is fully thread-safe and runs in a thread pool.
        T2 (Ivy parser) runs sequentially afterwards because the parser
        may have global state.
        """
        num_workers = get_config().bulk_workers

        # Phase A: thread-parallel T1
        sources: Dict[str, str] = {}
        t1_annotations_map: Dict[str, List[Any]] = {}

        def _t1_task(
            filepath: str,
        ) -> Tuple[str, Optional[str], Optional[List[Any]], Optional[str]]:
            """Return (filepath, source_or_None, annotations_or_None, error_or_None)."""
            try:
                with open(filepath) as f:
                    src = f.read()
            except OSError as exc:
                return filepath, None, None, str(exc)
            try:
                anns = self.run_tier1(src, filepath)
            except Exception as exc:
                return filepath, src, None, f"T1: {exc}"
            return filepath, src, anns, None

        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            return

        completed = 0
        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures: Dict[Any, str] = {}
            for fp in remaining:
                if cancel_event is not None and cancel_event.is_set():
                    break
                futures[pool.submit(_t1_task, fp)] = fp
            for future in as_completed(futures):
                if cancel_event is not None and cancel_event.is_set():
                    result.cancelled = True
                    break
                filepath, src, anns, error = future.result()
                if src is not None:
                    sources[filepath] = src
                if anns is not None:
                    t1_annotations_map[filepath] = anns
                if error is not None:
                    result.errors.append((filepath, error))
                else:
                    result.t1_completed += 1
                completed += 1
                self._bulk_progress_tick(
                    completed - 1,
                    len(remaining),
                    filepath,
                    progress_callback,
                )

        if result.cancelled:
            return

        # Phase B: sequential T2 (parser may have global state)
        if include_t2:
            for filepath in remaining:
                if cancel_event is not None and cancel_event.is_set():
                    result.cancelled = True
                    break
                source = sources.get(filepath)
                if source is None:
                    continue
                parse_result = None
                try:
                    parse_result = self._parser.parse(source, filepath)
                except Exception as exc:
                    logger.debug("Bulk T2 parse failed for %s: %s", filepath, exc)
                    result.errors.append((filepath, f"T2-parse: {exc}"))
                try:
                    self.run_tier2(
                        source,
                        filepath,
                        parse_result=parse_result,
                        rfc_annotations=t1_annotations_map.get(filepath),
                    )
                    result.t2_completed += 1
                except Exception as exc:
                    result.errors.append((filepath, f"T2: {exc}"))

    # -- Bulk Tier 3 (workspace-wide compilation) ------------------------------

    def run_bulk_tier3(
        self,
        test_files: List[str],
        progress_callback: Optional[Callable[[int, int, Optional[str]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        """Run Tier 3 compilation for all test entry points.

        Iterates *test_files*, compiles each via ``CompilerManager.compile_async``,
        and enriches both RequirementGraph and SemanticModel on completion.
        Tracks ``_bulk_compile`` state for monitoring endpoints.

        Args:
            test_files: Absolute paths to test ``.ivy`` files.
            progress_callback: Called after each file with ``(completed, total, filepath)``.
            cancel_event: If set, no further compilations are submitted.
        """
        if self._compiler_manager is None:
            logger.info("Bulk T3 skipped: no CompilerManager available")
            return

        with self._state_lock:
            if self._bulk_compile.running:
                logger.info("Bulk T3 already running, skipping")
                return
            self._bulk_compile.running = True
            self._bulk_compile.total = len(test_files)
            self._bulk_compile.completed = 0
            self._bulk_compile.cancelled = False

        if not test_files:
            with self._state_lock:
                self._bulk_compile.running = False
            return

        completed_count = [0]
        total = len(test_files)
        last_notify_time = [0.0]
        submitted_count = [0]

        def _make_callback(filepath: str):
            with self._state_lock:
                gen_at_submit = self._file_generation.get(filepath, 0)

            def _on_compile(ir):
                with self._state_lock:
                    current_gen = self._file_generation.get(filepath, 0)
                if current_gen != gen_at_submit:
                    logger.debug(
                        "Discarding stale bulk T3 result for %s (gen %d vs %d)",
                        filepath,
                        gen_at_submit,
                        current_gen,
                    )
                    # Still count completion for progress so bulk compilation can finish
                    with self._state_lock:
                        completed_count[0] += 1
                        self._bulk_compile.completed = completed_count[0]
                        stale_is_final = completed_count[0] >= submitted_count[0]
                    if stale_is_final:
                        with self._state_lock:
                            self._bulk_compile.running = False
                        logger.info(
                            "Bulk T3 compilation complete (stale final): %d/%d files",
                            completed_count[0],
                            total,
                        )
                    return

                with self._state_lock:
                    completed_count[0] += 1
                    current = completed_count[0]
                    self._bulk_compile.completed = current
                    # Throttle check under lock to prevent bursts
                    now = time.time()
                    is_final = current >= submitted_count[0]
                    should_notify = is_final or (now - last_notify_time[0]) >= 1.0
                    if should_notify:
                        last_notify_time[0] = now

                if ir.success:
                    try:
                        from ivy_lsp.compilation.graph_enrichment import (
                            enrich_requirement_graph,
                            enrich_semantic_model,
                        )

                        if self._requirement_graph is not None:
                            enrich_requirement_graph(self._requirement_graph, ir)
                        enrich_semantic_model(self._model, ir, filepath)
                    except Exception:
                        logger.warning(
                            "Bulk T3 enrichment failed for %s",
                            filepath,
                            exc_info=True,
                        )

                if should_notify:
                    if progress_callback is not None:
                        try:
                            progress_callback(current, total, filepath)
                        except Exception:
                            logger.debug(
                                "Bulk T3 progress callback failed", exc_info=True
                            )

                    if self._notification_callback is not None:
                        try:
                            self._notification_callback(
                                current, total, filepath, ir.success
                            )
                        except Exception:
                            logger.debug(
                                "Bulk T3 notification callback failed",
                                exc_info=True,
                            )

                if is_final:
                    with self._state_lock:
                        self._bulk_compile.running = False
                    logger.info(
                        "Bulk T3 compilation complete: %d/%d files",
                        current,
                        total,
                    )

            return _on_compile

        for test_file in test_files:
            if cancel_event is not None and cancel_event.is_set():
                with self._state_lock:
                    self._bulk_compile.running = False
                    self._bulk_compile.cancelled = True
                break

            try:
                with open(test_file) as f:
                    source = f.read()
            except OSError:
                logger.warning("Cannot read %s for bulk T3; skipping", test_file)
                if progress_callback is not None:
                    try:
                        progress_callback(completed_count[0], total, test_file)
                    except Exception:
                        logger.debug(
                            "Bulk T3 progress callback failed in skip path",
                            exc_info=True,
                        )
                continue

            submitted_count[0] += 1
            self._compiler_manager.compile_async(
                source, test_file, _make_callback(test_file)
            )

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
            self._tier3.last_file = filepath
            self._tier3.last_completed_at = completed_at
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

        Acquires ``_state_lock`` to get a consistent snapshot of T3,
        bulk analysis, and bulk compilation state.
        """
        with self._state_lock:
            succeeded = sum(1 for r in self._tier3_results.values() if r.success)
            failed = sum(1 for r in self._tier3_results.values() if not r.success)
            t3_count = len(self._tier3_results)
            t3_running = self._tier3.running
            t3_current = self._tier3.current_file
            t3_last = self._tier3.last_file
            t3_last_at = self._tier3.last_completed_at
            t3_pending = self._tier3.pending
            bulk_running = self._bulk.running
            bulk_total = self._bulk.total
            bulk_completed = self._bulk.completed
            t1_count = len(self._tier1_files)
            t2_count = len(self._tier2_files)
            bulk_compile_running = self._bulk_compile.running
            bulk_compile_total = self._bulk_compile.total
            bulk_compile_completed = self._bulk_compile.completed
            bulk_compile_cancelled = self._bulk_compile.cancelled

        # Compiler manager stats (cache/process counts)
        mgr_stats = {"cachedFiles": 0, "activeProcesses": 0, "maxConcurrent": 0}
        if self._compiler_manager is not None:
            try:
                mgr_stats = self._compiler_manager.get_stats()
            except Exception:
                logger.debug("CompilerManager.get_stats() failed", exc_info=True)

        return {
            "tier1FileCount": t1_count,
            "tier2FileCount": t2_count,
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
            "bulkCompileRunning": bulk_compile_running,
            "bulkCompileTotal": bulk_compile_total,
            "bulkCompileCompleted": bulk_compile_completed,
            "bulkCompileCancelled": bulk_compile_cancelled,
            **mgr_stats,
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
            t1_annotations = self.run_tier1(source, filepath)
            parse_result = self.run_tier2(
                source, filepath, rfc_annotations=t1_annotations
            )
        if trigger in ("save", "command"):
            t3_source, t3_filepath = source, filepath
            redirected = False
            if self._test_file_resolver is not None:
                test_file = self._test_file_resolver(filepath)
                if test_file is not None:
                    try:
                        with open(test_file) as f:
                            t3_source = f.read()
                        t3_filepath = test_file
                        redirected = True
                        logger.debug(
                            "T3 redirected from %s to enclosing test %s",
                            filepath,
                            test_file,
                        )
                    except OSError:
                        logger.debug("T3 redirect failed: cannot read %s", test_file)
            # Non-test module files cannot compile standalone — skip T3
            if not redirected and not re.search(r"^\s*export\s", source, re.MULTILINE):
                logger.debug("T3 skipped for module %s: no enclosing test", filepath)
                return parse_result
            self.run_tier3_background(t3_source, t3_filepath)
        return parse_result
