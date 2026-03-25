"""Diagnostic computation functions extracted from diagnostics.py.

This module contains the pure compute functions for generating LSP
diagnostics from Ivy source files.  The cache layer, registration
handlers, and push/pull plumbing remain in ``diagnostics.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional

from lsprotocol import types as lsp

from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})

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


def check_structural_issues(
    source: str,
    filepath: str,
    indexer: Any = None,
) -> List[lsp.Diagnostic]:
    """Check for structural problems without full parsing."""
    from ivy_lsp.infra.utils.structural_lint import (
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
    if resolve_cb is not None:
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
                        f"High-impact state variable '{var_name}': read by "
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

    from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
    from ivy_lsp.core.semantic.rfc_annotations import is_tag_covered

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
    from ivy_lsp.features.diagnostics import _convert_error_to_diagnostic

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
            from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

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
    from ivy_lsp.infra.utils.ivy_output import parse_ivy_check_lines

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
