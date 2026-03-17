"""Diagnostics feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.structured_logging import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.utils import uri_to_path

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})

DEBOUNCE_DELAY = 0.5  # seconds

# Pre-compiled regex patterns for hot-path performance (Phase 2.1)
_INCLUDE_RE = re.compile(r"^include\s+(\w+)", re.MULTILINE)
_ACTION_RE = re.compile(r"^\s*action\s+([\w.]+)", re.MULTILINE)
_STATE_VAR_RE = re.compile(
    r"^\s*(?:relation|function|individual|var)\s+([\w.]+)", re.MULTILINE
)
_ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)
_TAG_RE = re.compile(r"#\s*\[")
_EXPORT_RE = re.compile(r"^\s*export\s", re.MULTILINE)


@dataclass
class _CachedDiagnosticEntry:
    """Per-URI cached diagnostic result for pull diagnostics."""

    result_id: str
    source_hash: str
    diagnostics: List[lsp.Diagnostic]
    deep_diagnostics: Optional[List[lsp.Diagnostic]] = None


class DiagnosticCache:
    """Thread-safe per-URI diagnostic cache for LSP 3.17 pull diagnostics.

    Stores fast diagnostics and deep diagnostics (ivy_check) separately.
    ``result_id`` is derived from the source hash + a suffix indicating
    whether deep diagnostics are present, so pull clients will re-request
    when deep results arrive.
    """

    def __init__(self) -> None:
        """Initialize an empty diagnostic cache."""
        self._lock = Lock()
        self._entries: Dict[str, _CachedDiagnosticEntry] = {}

    @staticmethod
    def _hash(source: str) -> str:
        return hashlib.sha256(source.encode()).hexdigest()[:16]

    @staticmethod
    def _result_id(source_hash: str, has_deep: bool) -> str:
        return source_hash + ("-deep" if has_deep else "-fast")

    def update_fast(self, uri: str, source: str, diags: List[lsp.Diagnostic]) -> str:
        """Update fast diagnostics for *uri*.  Returns the new ``result_id``."""
        h = self._hash(source)
        with self._lock:
            existing = self._entries.get(uri)
            # Preserve deep diagnostics if source hasn't changed
            deep = (
                existing.deep_diagnostics
                if existing and existing.source_hash == h
                else None
            )
            rid = self._result_id(h, deep is not None)
            self._entries[uri] = _CachedDiagnosticEntry(rid, h, diags, deep)
            return rid

    def update_deep(self, uri: str, deep: List[lsp.Diagnostic]) -> Optional[str]:
        """Update deep diagnostics overlay.  Returns new ``result_id`` or ``None``."""
        with self._lock:
            e = self._entries.get(uri)
            if e is None:
                return None
            e.deep_diagnostics = deep
            e.result_id = self._result_id(e.source_hash, True)
            return e.result_id

    def get(self, uri: str) -> Optional[_CachedDiagnosticEntry]:
        """Return the cached entry for *uri*, or ``None``."""
        with self._lock:
            return self._entries.get(uri)

    def get_merged(self, uri: str) -> Optional[Tuple[str, List[lsp.Diagnostic]]]:
        """Return ``(result_id, fast + deep diagnostics)`` or ``None``."""
        with self._lock:
            e = self._entries.get(uri)
            if e is None:
                return None
            merged = list(e.diagnostics)
            if e.deep_diagnostics:
                merged.extend(e.deep_diagnostics)
            return (e.result_id, merged)

    def invalidate(self, uri: str) -> None:
        """Remove cached entry for *uri*."""
        with self._lock:
            self._entries.pop(uri, None)

    def all_uris(self) -> List[str]:
        """Return all cached URIs."""
        with self._lock:
            return list(self._entries.keys())


def _convert_error_to_diagnostic(error: Any, source: str) -> lsp.Diagnostic:
    """Convert a single Ivy parse error to an LSP Diagnostic.

    Handles three error representations:
    - Error objects with ``.msg`` and ``.lineno`` attributes (IvyError)
    - Raw parser tuples ``(symbol_name, loc1, loc2, ...)``, formatted
      via ``format_ivy_error()`` with line extracted from first location
    - Generic fallback: ``str(error)``
    """
    from ivy_lsp.utils.ivy_output import format_ivy_error

    line = 0
    message = str(error)

    if hasattr(error, "lineno"):
        lineno = error.lineno
        if hasattr(lineno, "line") and isinstance(lineno.line, int) and lineno.line > 0:
            line = lineno.line - 1

    if hasattr(error, "msg"):
        message = error.msg
    elif isinstance(error, tuple) and len(error) >= 1 and isinstance(error[0], str):
        # Raw parser tuples: (symbol_name, loc1, loc2, ...)
        message = format_ivy_error(error)
        # Use the first location with a valid line number
        for loc in error[1:]:
            if isinstance(loc, tuple) and len(loc) >= 2:
                if isinstance(loc[1], int) and loc[1] > 0:
                    line = loc[1] - 1
                    break

    lines = source.split("\n")
    line_len = len(lines[line]) if line < len(lines) else 0

    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=line, character=0),
            end=lsp.Position(line=line, character=line_len),
        ),
        message=message,
        severity=lsp.DiagnosticSeverity.Error,
        source="ivy",
    )


def check_structural_issues(
    source: str,
    filepath: str,
    indexer: Any = None,
) -> List[lsp.Diagnostic]:
    """Check for structural problems without full parsing."""
    from ivy_lsp.utils.structural_lint import (
        check_structural_issues_raw,
        check_unresolved_includes_raw,
    )

    raw = check_structural_issues_raw(source, filepath)

    # Include resolution: use partition-aware resolver when available,
    # falling back to the default resolver.
    resolve_cb = None
    if indexer:
        resolver = indexer.resolver
        # Check for real IncludeResolver with active partition staging.
        partition_staging = getattr(resolver, "_partition_staging", None)
        if isinstance(partition_staging, dict) and partition_staging:
            resolve_cb = resolver.resolve_partitioned
        else:
            resolve_cb = resolver.resolve
    raw.extend(
        check_unresolved_includes_raw(source, filepath, resolve_callback=resolve_cb)
    )

    lines = source.split("\n")
    diags: List[lsp.Diagnostic] = []
    for entry in raw:
        lineno = max(0, entry["line"] - 1)  # convert 1-based to 0-based
        line_text = lines[lineno] if lineno < len(lines) else ""
        severity = (
            lsp.DiagnosticSeverity.Error
            if entry["severity"] == "error"
            else lsp.DiagnosticSeverity.Warning
        )
        diags.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(lineno, 0),
                    end=lsp.Position(lineno, len(line_text)),
                ),
                message=entry["message"],
                severity=severity,
                source="ivy-lsp",
                code=entry.get("code"),
            )
        )
    return diags


def compute_requirement_diagnostics(
    source: str,
    filepath: str,
    indexer: Any = None,
) -> List[lsp.Diagnostic]:
    """Compute requirement-analysis diagnostics for a source file.

    Emits diagnostics for:
    1. Include chain propagation (Info)
    2. Unmonitored actions (Hint)
    3. High-impact state variables (Info)
    """
    if indexer is None:
        return []

    graph = getattr(indexer, "requirement_graph", None)
    include_graph = getattr(indexer, "include_graph", None)
    if graph is None:
        return []

    import os

    diags: List[lsp.Diagnostic] = []
    abs_path = os.path.abspath(filepath)
    lines = source.split("\n")

    # 1. Include chain propagation (per-include deduplicated counts)
    if include_graph:
        resolver = getattr(indexer, "resolver", None)
        seen_files: set = set()

        for match in _INCLUDE_RE.finditer(source):
            inc_name = match.group(1)
            line_no = source[: match.start()].count("\n")
            line_text = lines[line_no] if line_no < len(lines) else ""

            # Resolve include to file path
            resolved_path = None
            if resolver is not None:
                resolved_path = resolver.resolve(inc_name, abs_path)
            if resolved_path is None:
                continue

            # Transitive closure (resolved file + its includes)
            transitive_files = include_graph.get_transitive_includes(resolved_path) | {
                resolved_path
            }

            # Deduplicate: only count files not yet claimed
            new_files = transitive_files - seen_files
            seen_files |= new_files

            # Count requirements in newly-introduced files
            inherited_reqs = [
                r for r in graph.requirements.values() if r.file in new_files
            ]

            if not inherited_reqs:
                continue

            related = [
                lsp.DiagnosticRelatedInformation(
                    location=lsp.Location(
                        uri=f"file://{req.file}",
                        range=lsp.Range(
                            start=lsp.Position(req.line, 0),
                            end=lsp.Position(req.line, 80),
                        ),
                    ),
                    message=f"{req.kind}: {req.formula_text[:60]}",
                )
                for req in inherited_reqs
            ]

            diags.append(
                lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(line_no, 0),
                        end=lsp.Position(line_no, len(line_text)),
                    ),
                    message=(
                        f"Brings {len(inherited_reqs)} requirements into scope "
                        f"from {inc_name} (and transitive includes)"
                    ),
                    severity=lsp.DiagnosticSeverity.Information,
                    source="ivy-lsp-reqs",
                    related_information=related[:10],
                )
            )

    # 2. Unmonitored actions (Hint)
    active_scope = None
    if isinstance(graph, ScopedRequirementModel):
        active_scope = graph.get_active_scope()

    for match in _ACTION_RE.finditer(source):
        action_name = match.group(1)
        line_no = source[: match.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""

        if active_scope is not None:
            if not active_scope.is_action_exported(action_name):
                continue
            scoped_counts = graph.get_scoped_counts(active_scope.test_file, action_name)
            if not scoped_counts:
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line_no, 0),
                            end=lsp.Position(line_no, len(line_text)),
                        ),
                        message=(
                            f"Action '{action_name}' has no before/after "
                            f"monitors in active test scope"
                        ),
                        severity=lsp.DiagnosticSeverity.Hint,
                        source="ivy-lsp-reqs",
                    )
                )
        else:
            reqs = graph.get_requirements_for_action(action_name)
            if not reqs:
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line_no, 0),
                            end=lsp.Position(line_no, len(line_text)),
                        ),
                        message=(
                            f"Action '{action_name}' has no before/after "
                            f"monitors in scope"
                        ),
                        severity=lsp.DiagnosticSeverity.Hint,
                        source="ivy-lsp-reqs",
                    )
                )

    # 3. High-impact state variables (Info, threshold: 5+ readers)
    impact_threshold = 5
    for match in _STATE_VAR_RE.finditer(source):
        var_name = match.group(1)
        line_no = source[: match.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""

        readers = graph.get_requirements_sharing_state_var(var_name)
        if len(readers) >= impact_threshold:
            files = {r.file for r in readers}
            diags.append(
                lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(line_no, 0),
                        end=lsp.Position(line_no, len(line_text)),
                    ),
                    message=(
                        f"High-impact state variable: read by "
                        f"{len(readers)} requirements across {len(files)} files"
                    ),
                    severity=lsp.DiagnosticSeverity.Information,
                    source="ivy-lsp-reqs",
                )
            )

    return diags


def compute_semantic_diagnostics(
    model: Any,
    filepath: str,
    source: str,
) -> List[lsp.Diagnostic]:
    """Compute diagnostics from the SemanticModel.

    Categories:
    - Orphaned RFC tags (Warning): bracket tags not matching any manifest
    - Missing tags on assertions (Hint): require/ensure without bracket tag
    """
    if model is None:
        return []

    import os

    from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement
    from ivy_lsp.semantic.rfc_annotations import is_tag_covered

    diags: List[lsp.Diagnostic] = []
    abs_path = os.path.abspath(filepath)
    lines = source.split("\n")

    # Collect RFC requirements and annotations from the model
    rfc_reqs = model.get_nodes_by_type(RfcRequirement)
    annotations = [
        n for n in model.get_nodes_by_type(RfcAnnotation) if n.file == abs_path
    ]

    if rfc_reqs:
        req_ids = {r.id for r in rfc_reqs}

        # Orphaned RFC tags: bracket tags that don't match any manifest requirement
        for ann in annotations:
            for tag in ann.tags:
                if not is_tag_covered(tag, req_ids):
                    line = ann.line
                    line_len = len(lines[line]) if line < len(lines) else 0
                    diags.append(
                        lsp.Diagnostic(
                            range=lsp.Range(
                                start=lsp.Position(line, 0),
                                end=lsp.Position(line, line_len),
                            ),
                            message=(
                                f"Orphaned RFC tag: [{tag}] does not match "
                                "any loaded requirement manifest"
                            ),
                            severity=lsp.DiagnosticSeverity.Warning,
                            source="ivy-lsp-semantic",
                        )
                    )

    # Missing tags on assertions (Hint)
    for m in _ASSERTION_RE.finditer(source):
        line_no = source[: m.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""
        if not _TAG_RE.search(line_text):
            diags.append(
                lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(line_no, 0),
                        end=lsp.Position(line_no, len(line_text)),
                    ),
                    message="Assertion without RFC bracket tag annotation",
                    severity=lsp.DiagnosticSeverity.Hint,
                    source="ivy-lsp-semantic",
                )
            )

    return diags


def compute_diagnostics(
    parser: Any,
    source: str,
    filepath: str,
    indexer: Any = None,
    semantic_model: Any = None,
    parse_result: Any = None,
) -> List[lsp.Diagnostic]:
    """Compute all diagnostics for a source file.

    If *parse_result* is provided (e.g. from a preceding pipeline.analyze()
    call), it is reused instead of invoking the parser again, avoiding
    redundant lock acquisition on ``_ivy_state_lock``.
    """
    diags = check_structural_issues(source, filepath, indexer)

    if parser is None and parse_result is None:
        return diags

    result = (
        parse_result
        if parse_result is not None
        else (parser.parse(source, filepath) if parser is not None else None)
    )
    if result is None:
        return diags
    if not result.success:
        for error in result.errors:
            diags.append(_convert_error_to_diagnostic(error, source))

        # Surface fallback scanner lexer errors as diagnostics.
        # Skip if the parse result already carries lexer diagnostics
        # (avoids redundant re-scan of the same source).
        if not getattr(result, "lexer_errors", None):
            from ivy_lsp.parsing.fallback_scanner import fallback_scan

            _symbols, error_info = fallback_scan(source, filepath)
            if error_info is not None:
                err_line = max(0, error_info.get("line", 1) - 1)
                err_msg = error_info.get("message", "Lexer error")
                lines = source.split("\n")
                line_len = len(lines[err_line]) if err_line < len(lines) else 0
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=err_line, character=0),
                            end=lsp.Position(line=err_line, character=line_len),
                        ),
                        message=f"Lexer error: {err_msg}",
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lsp",
                    )
                )

    # Requirement analysis diagnostics
    req_diags = compute_requirement_diagnostics(source, filepath, indexer)
    diags.extend(req_diags)

    # Semantic model diagnostics
    sem_diags = compute_semantic_diagnostics(semantic_model, filepath, source)
    diags.extend(sem_diags)

    # Coverage hint diagnostics (C1)
    if indexer is not None:
        graph = getattr(indexer, "requirement_graph", None)
        if graph is not None:
            from ivy_lsp.features.coverage_hints import compute_coverage_hints

            lines = source.split("\n")
            for hint in compute_coverage_hints(graph, filepath):
                line = hint.get("line", 0)
                line_len = len(lines[line]) if line < len(lines) else 0
                sev_map = {
                    "hint": lsp.DiagnosticSeverity.Hint,
                    "info": lsp.DiagnosticSeverity.Information,
                    "warning": lsp.DiagnosticSeverity.Warning,
                    "error": lsp.DiagnosticSeverity.Error,
                }
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=line, character=0),
                            end=lsp.Position(line=line, character=line_len),
                        ),
                        message=hint["message"],
                        severity=sev_map.get(
                            hint.get("severity", "hint"),
                            lsp.DiagnosticSeverity.Hint,
                        ),
                        source="ivy-lsp-coverage",
                        code=hint.get("code"),
                        tags=[lsp.DiagnosticTag.Unnecessary],
                    )
                )

    # --- Pattern diagnostics (cheap regex checks) ---
    try:
        basename = os.path.basename(filepath)

        # Check for missing _finalize in test files
        if "test" in basename.lower() and "_finalize" not in source:
            # Look for export action declarations
            has_export = bool(re.search(r"^\s*export\s+action", source, re.MULTILINE))
            if has_export:
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line=0, character=0),
                            end=lsp.Position(line=0, character=1),
                        ),
                        message="Test file has exports but no _finalize action. "
                        "Consider adding 'export action _finalize' for end-of-test assertions.",
                        severity=lsp.DiagnosticSeverity.Warning,
                        source="ivy-pattern",
                    )
                )

        # Check for exported actions without monitors
        exports = set(
            re.findall(r"^\s*export\s+action\s+([\w.]+)", source, re.MULTILINE)
        )
        monitored = set(
            re.findall(r"^\s*(?:before|after|around)\s+([\w.]+)", source, re.MULTILINE)
        )
        for exp_action in exports:
            if exp_action not in monitored and exp_action != "_finalize":
                # Only warn if this file also defines the action
                action_defined = bool(
                    re.search(
                        rf"^\s*action\s+{re.escape(exp_action)}\s*",
                        source,
                        re.MULTILINE,
                    )
                )
                if action_defined:
                    match = re.search(
                        rf"^\s*export\s+action\s+{re.escape(exp_action)}",
                        source,
                        re.MULTILINE,
                    )
                    line_num = source[: match.start()].count("\n") if match else 0
                    diags.append(
                        lsp.Diagnostic(
                            range=lsp.Range(
                                start=lsp.Position(line=line_num, character=0),
                                end=lsp.Position(line=line_num, character=80),
                            ),
                            message=f"Exported action '{exp_action}' has no before/after monitor in this file.",
                            severity=lsp.DiagnosticSeverity.Hint,
                            source="ivy-pattern",
                        )
                    )
    except Exception:
        pass  # Don't let pattern checks break diagnostics

    return diags


def parse_ivy_check_output(output: str) -> List[lsp.Diagnostic]:
    """Parse ivy_check stderr/stdout into LSP diagnostics."""
    from ivy_lsp.utils.ivy_output import parse_ivy_check_lines

    diags: List[lsp.Diagnostic] = []
    for entry in parse_ivy_check_lines(output):
        lineno = max(0, entry["line"] - 1)
        severity = (
            lsp.DiagnosticSeverity.Error
            if entry["severity"] == "error"
            else lsp.DiagnosticSeverity.Warning
        )
        diags.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(lineno, 0),
                    end=lsp.Position(lineno + 1, 0),
                ),
                message=entry["message"],
                severity=severity,
                source="ivy_check",
            )
        )
    return diags


async def run_deep_diagnostics(
    filepath: str,
    ivy_check_cmd: str = "ivy_check",
    cwd: Optional[str] = None,
) -> List[lsp.Diagnostic]:
    """Run ivy_check as subprocess and convert output to diagnostics."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ivy_check_cmd,
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except FileNotFoundError:
        slog.info(
            "%s not found on PATH",
            ivy_check_cmd,
            extra={
                "event": LogEvent(
                    LogCategory.DIAGNOSTIC,
                    "diagnostics",
                    {"tool": ivy_check_cmd},
                )
            },
        )
        return []
    except asyncio.TimeoutError:
        slog.warning(
            "Deep diagnostics timed out for %s",
            filepath,
            extra={
                "event": LogEvent(
                    LogCategory.DIAGNOSTIC,
                    "diagnostics",
                    {"filepath": filepath},
                )
            },
        )
        return []
    except Exception:
        logger.warning("Deep diagnostics failed for %s", filepath, exc_info=True)
        return []

    output = stderr.decode("utf-8", errors="replace") + stdout.decode(
        "utf-8", errors="replace"
    )
    return parse_ivy_check_output(output)


def register(server) -> None:
    """Register diagnostic handlers for didOpen, didChange, didSave, and didClose."""
    _debounce_tasks: Dict[str, asyncio.Task] = {}
    _deep_tasks: Dict[str, asyncio.Task] = {}

    def _get_semantic_model():
        return getattr(server, "semantic_model", None)

    def _run_pipeline(source: str, filepath: str, trigger: str) -> Any:
        """Run the analysis pipeline. Returns the ParseResult (or None)."""
        pipeline = getattr(server, "analysis_pipeline", None)
        if pipeline:
            try:
                return pipeline.analyze(source, filepath, trigger)
            except Exception:
                logger.warning(
                    "Pipeline analysis failed for %s", filepath, exc_info=True
                )
        return None

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    async def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        filepath = uri_to_path(uri)
        source = doc.source or ""
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, _run_pipeline, source, filepath, "change"
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )
        server.diagnostic_cache.update_fast(uri, source, diags)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=uri, version=doc.version, diagnostics=diags
            )
        )

    @server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    async def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
        uri = params.text_document.uri
        old_task = _debounce_tasks.pop(uri, None)
        if old_task and not old_task.done():
            old_task.cancel()

        async def _debounced():
            try:
                await asyncio.sleep(DEBOUNCE_DELAY)
                doc = server.workspace.get_text_document(uri)
                filepath = uri_to_path(uri)
                source = doc.source or ""
                loop = asyncio.get_running_loop()
                pipeline_result = await loop.run_in_executor(
                    None, _run_pipeline, source, filepath, "change"
                )
                diags = await loop.run_in_executor(
                    None,
                    compute_diagnostics,
                    server.parser,
                    source,
                    filepath,
                    server.indexer,
                    _get_semantic_model(),
                    pipeline_result,
                )
                server.diagnostic_cache.update_fast(uri, source, diags)
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(
                        uri=uri, version=doc.version, diagnostics=diags
                    )
                )
            except asyncio.CancelledError:
                logger.debug("Debounced diagnostics task cancelled for %s", uri)
            except Exception:
                logger.warning(
                    "Debounced diagnostics failed for %s", uri, exc_info=True
                )

        loop = asyncio.get_running_loop()
        task = loop.create_task(_debounced())
        task.add_done_callback(
            lambda t, u=uri: (
                _debounce_tasks.pop(u, None) if _debounce_tasks.get(u) is t else None
            )
        )
        _debounce_tasks[uri] = task

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    async def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
        uri = params.text_document.uri
        filepath = uri_to_path(uri)
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None, _run_pipeline, source, filepath, "save"
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )
        server.diagnostic_cache.update_fast(uri, source, diags)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=uri, version=doc.version, diagnostics=diags
            )
        )

        if filepath.endswith(".ivy") and server.indexer is not None:
            await loop.run_in_executor(
                None, server.indexer.reindex_file_with_dependents, filepath
            )

        doc_version = doc.version

        async def _deep():
            try:
                import os

                from ivy_lsp.features.commands import (
                    _find_enclosing_test,
                    _resolve_via_staging,
                )

                enclosing = _find_enclosing_test(server, filepath)
                if enclosing is not None:
                    deep_filepath = _resolve_via_staging(server, enclosing)
                elif not _EXPORT_RE.search(source):
                    # Module file without test scope — can't compile standalone
                    return
                else:
                    deep_filepath = _resolve_via_staging(server, filepath)
                deep = await run_deep_diagnostics(
                    deep_filepath,
                    cwd=os.path.dirname(deep_filepath),
                )
                server.diagnostic_cache.update_deep(uri, deep)
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(
                        uri=uri, version=doc_version, diagnostics=diags + deep
                    )
                )
                # Notify pull-mode clients to re-request diagnostics
                try:
                    server.workspace_diagnostic_refresh(None)
                except Exception:
                    logger.debug("workspace/diagnostic/refresh not supported by client")
            except asyncio.CancelledError:
                logger.debug("Deep diagnostics task cancelled for %s", uri)
            except Exception:
                logger.warning(
                    "Deep diagnostics task failed for %s", uri, exc_info=True
                )

        # Cancel any prior deep task for this URI
        old_deep = _deep_tasks.pop(uri, None)
        if old_deep and not old_deep.done():
            old_deep.cancel()
        loop = asyncio.get_running_loop()
        deep_task = loop.create_task(_deep())
        deep_task.add_done_callback(
            lambda t, u=uri: (
                _deep_tasks.pop(u, None) if _deep_tasks.get(u) is t else None
            )
        )
        _deep_tasks[uri] = deep_task

    @server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
    def did_close(params: lsp.DidCloseTextDocumentParams) -> None:
        uri = params.text_document.uri
        # Cancel any pending debounce task for this URI
        old_task = _debounce_tasks.pop(uri, None)
        if old_task and not old_task.done():
            old_task.cancel()
        # Cancel any pending deep diagnostics task
        old_deep = _deep_tasks.pop(uri, None)
        if old_deep and not old_deep.done():
            old_deep.cancel()
        # Clear diagnostics for the closed document
        server.diagnostic_cache.invalidate(uri)
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )

    # -- LSP 3.17 Pull Diagnostics -----------------------------------------

    @server.feature(
        lsp.TEXT_DOCUMENT_DIAGNOSTIC,
        lsp.DiagnosticOptions(
            identifier="ivy",
            inter_file_dependencies=True,
            workspace_diagnostics=True,
        ),
    )
    async def text_document_diagnostic(
        params: lsp.DocumentDiagnosticParams,
    ) -> (
        lsp.RelatedFullDocumentDiagnosticReport
        | lsp.RelatedUnchangedDocumentDiagnosticReport
    ):
        uri = params.text_document.uri
        cache = server.diagnostic_cache

        # Fast path: unchanged
        if params.previous_result_id is not None:
            entry = cache.get(uri)
            if entry is not None and entry.result_id == params.previous_result_id:
                return lsp.RelatedUnchangedDocumentDiagnosticReport(
                    result_id=entry.result_id,
                )

        # Get source (open doc or disk)
        try:
            doc = server.workspace.get_text_document(uri)
            source = doc.source or ""
        except KeyError:
            filepath = uri_to_path(uri)
            try:
                with open(filepath, "r") as f:
                    source = f.read()
            except OSError:
                return lsp.RelatedFullDocumentDiagnosticReport(
                    items=[],
                    result_id="empty",
                )

        filepath = uri_to_path(uri)
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            _run_pipeline,
            source,
            filepath,
            "pull",
        )
        diags = await loop.run_in_executor(
            None,
            compute_diagnostics,
            server.parser,
            source,
            filepath,
            server.indexer,
            _get_semantic_model(),
            pipeline_result,
        )

        result_id = cache.update_fast(uri, source, diags)
        # Merge cached deep diagnostics if available
        entry = cache.get(uri)
        if entry and entry.deep_diagnostics:
            diags = diags + entry.deep_diagnostics
            result_id = entry.result_id

        return lsp.RelatedFullDocumentDiagnosticReport(
            items=diags,
            result_id=result_id,
        )

    @server.feature(lsp.WORKSPACE_DIAGNOSTIC)
    async def workspace_diagnostic(
        params: lsp.WorkspaceDiagnosticParams,
    ) -> lsp.WorkspaceDiagnosticReport:
        cache = server.diagnostic_cache
        items: list = []

        # Build lookup of previous result IDs
        prev_ids: Dict[str, str] = {}
        for prev in params.previous_result_ids:
            prev_ids[prev.uri] = prev.value

        # Collect URIs: open docs + indexed workspace files
        uris: set = set()
        for doc_uri in server.workspace.text_documents:
            uris.add(doc_uri)
        if server.indexer is not None:
            for fp in server.indexer.get_all_ivy_file_paths():
                uris.add(f"file://{fp}")

        for uri in sorted(uris):
            prev_rid = prev_ids.get(uri)

            # Unchanged check
            if prev_rid is not None:
                entry = cache.get(uri)
                if entry is not None and entry.result_id == prev_rid:
                    items.append(
                        lsp.WorkspaceUnchangedDocumentDiagnosticReport(
                            uri=uri,
                            version=None,
                            result_id=entry.result_id,
                        )
                    )
                    continue

            # Return cached if available
            cached = cache.get_merged(uri)
            if cached is not None:
                rid, merged = cached
                items.append(
                    lsp.WorkspaceFullDocumentDiagnosticReport(
                        uri=uri,
                        items=merged,
                        version=None,
                        result_id=rid,
                    )
                )
                continue

            # Compute fresh (fallback for uncached files)
            filepath = uri_to_path(uri)
            try:
                doc = server.workspace.get_text_document(uri)
                source = doc.source or ""
            except KeyError:
                try:
                    with open(filepath, "r") as f:
                        source = f.read()
                except OSError:
                    continue

            loop = asyncio.get_running_loop()
            diags = await loop.run_in_executor(
                None,
                compute_diagnostics,
                server.parser,
                source,
                filepath,
                server.indexer,
                _get_semantic_model(),
                None,
            )
            rid = cache.update_fast(uri, source, diags)
            items.append(
                lsp.WorkspaceFullDocumentDiagnosticReport(
                    uri=uri,
                    items=diags,
                    version=None,
                    result_id=rid,
                )
            )

        return lsp.WorkspaceDiagnosticReport(items=items)
