"""Diagnostic computation functions extracted from diagnostics.py.

This module contains the pure compute functions for generating LSP
diagnostics from Ivy source files.  The cache layer, registration
handlers, and push/pull plumbing remain in ``diagnostics.py``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from lsprotocol import types as lsp

from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.core.diagnostics.rich_diagnostic import IvyDiagnostic, RelatedLocation
from ivy_lsp.core.patterns import ASSERTION_RE as _ASSERTION_RE
from ivy_lsp.core.patterns import BRACKET_TAG_RE as _TAG_RE
from ivy_lsp.core.patterns import INCLUDE_RE as _INCLUDE_RE
from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})

# Pre-compiled regex patterns for hot-path performance (Phase 2.1)
_ACTION_RE = re.compile(r"^\s*action\s+([\w.]+)", re.MULTILINE)
_STATE_VAR_RE = re.compile(
    r"^\s*(?:relation|function|individual|var)\s+([\w.]+)", re.MULTILINE
)
_EXPORT_RE = re.compile(r"^\s*export\s", re.MULTILINE)

_IVY_CHECK_CODE_MAP: Dict[Tuple[str, str], str] = {
    ("cpp_compiler", "error"): "ivy.verify.compileError",
    ("cpp_compiler", "warning"): "ivy.verify.checkWarning",
}


def check_structural_issues(
    source: str,
    filepath: str,
    indexer: Any = None,
) -> List[lsp.Diagnostic]:
    """Check for structural problems without full parsing."""
    from ivy_lsp.core.structural_lint import (
        check_commented_out_requires,
        check_duplicate_tags,
        check_lowercase_params,
    )
    from ivy_lsp.core.structural_lint import (
        check_structural_issues as _core_check_structural_issues,
    )
    from ivy_lsp.core.structural_lint import check_unresolved_includes_raw

    # Core structural checks return IvyDiagnostic; convert to LSP at this boundary.
    core_diags = _core_check_structural_issues(source, filepath)
    # Graph-based coverage hints supersede the generic structural-lint
    # unguardedWrite emission; suppress here so coverage_hints can take over.
    if indexer is not None and getattr(indexer, "requirement_graph", None) is not None:
        core_diags = [d for d in core_diags if d.code != "ivy.action.unguardedWrite"]
    diags: List[lsp.Diagnostic] = [d.to_lsp() for d in core_diags]

    # Include resolution: use partition-aware resolver when available,
    # falling back to the default resolver.
    resolve_cb = None
    if indexer:
        resolver = indexer.resolver
        partition_staging = getattr(resolver, "_partition_staging", None)
        if isinstance(partition_staging, dict) and partition_staging:
            _partitioned = resolver.resolve_partitioned
            _full = resolver.resolve

            def resolve_cb(name, from_file):
                result = _partitioned(name, from_file)
                if result is None:
                    result = _full(name, from_file)
                return result

        else:
            resolve_cb = resolver.resolve

    # Helpers return IvyDiagnostic; convert to LSP at this boundary.
    helper_diags: list[IvyDiagnostic] = []
    if resolve_cb is not None:
        helper_diags.extend(
            check_unresolved_includes_raw(source, filepath, resolve_callback=resolve_cb)
        )
    helper_diags.extend(check_duplicate_tags(source, filepath))
    helper_diags.extend(check_commented_out_requires(source, filepath))
    helper_diags.extend(check_lowercase_params(source, filepath))

    diags.extend(d.to_lsp() for d in helper_diags)
    return diags


def compute_requirement_diagnostics(
    source: str,
    filepath: str,
    indexer: Any = None,
) -> List[IvyDiagnostic]:
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

    diags: List[IvyDiagnostic] = []
    abs_path = os.path.abspath(filepath)

    active_scope = None
    if isinstance(graph, ScopedRequirementModel):
        active_scope = graph.get_active_scope()

    # 1. Include chain propagation (per-include deduplicated counts)
    if include_graph:
        resolver = getattr(indexer, "resolver", None)
        seen_files: set = set()

        for match in _INCLUDE_RE.finditer(source):
            inc_name = match.group(1)
            line_no = source[: match.start()].count("\n")

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
            if active_scope is not None:
                scope_files = active_scope.include_closure
                inherited_reqs = [r for r in inherited_reqs if r.file in scope_files]

            if not inherited_reqs:
                continue

            related = [
                RelatedLocation(
                    file=req.file,
                    line=req.line,
                    message=f"{req.kind}: {req.formula_text[:60]}",
                )
                for req in inherited_reqs
            ]

            # Span the include name token (group 1 of INCLUDE_RE).
            line_start = source.rfind("\n", 0, match.start(1)) + 1
            char_start = match.start(1) - line_start
            char_end = match.end(1) - line_start
            diags.append(
                IvyDiagnostic(
                    code="ivy.module.inheritedRequirements",
                    message=(
                        f"Brings {len(inherited_reqs)} requirements into scope "
                        f"from {inc_name} (and transitive includes)"
                    ),
                    line=line_no,
                    character=char_start,
                    end_line=line_no,
                    end_character=char_end,
                    severity=lsp.DiagnosticSeverity.Information,
                    source="ivy-semantic",
                    related=related[:10],
                )
            )

    # 2. Unmonitored actions (Hint)
    for match in _ACTION_RE.finditer(source):
        action_name = match.group(1)
        line_no = source[: match.start()].count("\n")

        # Span the action name token (group 1 of _ACTION_RE).
        line_start = source.rfind("\n", 0, match.start(1)) + 1
        char_start = match.start(1) - line_start
        char_end = match.end(1) - line_start

        if active_scope is not None:
            if not active_scope.is_action_exported(action_name):
                continue
            scoped_counts = graph.get_scoped_counts(active_scope.test_file, action_name)
            if not scoped_counts:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.action.noMonitor",
                        message=(
                            f"Action '{action_name}' has no before/after "
                            f"monitors in active test scope"
                        ),
                        line=line_no,
                        character=char_start,
                        end_line=line_no,
                        end_character=char_end,
                        severity=lsp.DiagnosticSeverity.Hint,
                        source="ivy-semantic",
                    )
                )
        else:
            reqs = graph.get_requirements_for_action(action_name)
            if not reqs:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.action.noMonitor",
                        message=(
                            f"Action '{action_name}' has no before/after "
                            f"monitors in scope"
                        ),
                        line=line_no,
                        character=char_start,
                        end_line=line_no,
                        end_character=char_end,
                        severity=lsp.DiagnosticSeverity.Hint,
                        source="ivy-semantic",
                    )
                )

    # 3. High-impact state variables (Info, threshold: 5+ readers)
    impact_threshold = 5
    for match in _STATE_VAR_RE.finditer(source):
        var_name = match.group(1)
        line_no = source[: match.start()].count("\n")

        readers = graph.get_requirements_sharing_state_var(var_name)
        if active_scope is not None:
            scope_files = active_scope.include_closure
            readers = [r for r in readers if r.file in scope_files]
        if len(readers) >= impact_threshold:
            files = {r.file for r in readers}
            # Span the state-var name token (group 1 of _STATE_VAR_RE).
            line_start = source.rfind("\n", 0, match.start(1)) + 1
            char_start = match.start(1) - line_start
            char_end = match.end(1) - line_start
            diags.append(
                IvyDiagnostic(
                    code="ivy.invariant.highImpactVar",
                    message=(
                        f"High-impact state variable '{var_name}': read by "
                        f"{len(readers)} requirements across {len(files)} files"
                    ),
                    line=line_no,
                    character=char_start,
                    end_line=line_no,
                    end_character=char_end,
                    severity=lsp.DiagnosticSeverity.Information,
                    source="ivy-semantic",
                )
            )

    return diags


def _protocol_from_path(filepath: str) -> str | None:
    """Extract protocol name from a file path.

    Delegates to ``infer_protocol_from_path`` in ``_helpers.py``.

    Limitation: for APT-layout paths like
    ``protocol-testing/apt/apt_protocols/quic/...``, this returns
    ``"apt"`` rather than the actual protocol.
    """
    from ivy_lsp.mcp.tools._helpers import infer_protocol_from_path

    return infer_protocol_from_path(filepath)


def compute_semantic_diagnostics(
    model: Any,
    filepath: str,
    source: str,
) -> List[IvyDiagnostic]:
    """Compute diagnostics from the SemanticModel.

    Categories:
    - Orphaned RFC tags (Warning): bracket tags not matching any manifest
    - Missing tags on assertions (Hint): require/ensure without bracket tag
    """
    if model is None:
        return []

    from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement
    from ivy_lsp.core.semantic.rfc_annotations import is_tag_covered

    diags: List[IvyDiagnostic] = []
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
                        IvyDiagnostic(
                            code="ivy.rfc.orphanedTag",
                            message=(
                                f"Orphaned RFC tag: [{tag}] does not match "
                                "any loaded requirement manifest"
                            ),
                            line=line,
                            end_line=line,
                            end_character=line_len,
                            severity=lsp.DiagnosticSeverity.Warning,
                            source="ivy-lsp-semantic",
                        )
                    )

        # D6 + D7: Parse numeric tags once for both gap and duplicate detection.
        # D7 note: the spec calls for per-monitor-action grouping to suppress
        # cross-block duplicates, but that requires the RequirementGraph
        # (not available here — only SemanticModel). This file-level
        # version will flag cross-block duplicates as false positives
        # on files like quic_packet.ivy that repeat tags across handlers.
        file_tags: list[int] = []
        tag_to_line: dict[int, int] = {}
        seen_tags: dict[int, int] = {}
        for ann in annotations:
            for tag in ann.tags:
                parts = tag.split(":")
                numeric = parts[-1] if parts else tag
                try:
                    val = int(numeric)
                except ValueError:
                    continue
                file_tags.append(val)
                tag_to_line.setdefault(val, ann.line)

                # D7: flag duplicate tags within the file
                if val in seen_tags:
                    line_len = len(lines[ann.line]) if ann.line < len(lines) else 0
                    diags.append(
                        IvyDiagnostic(
                            code="ivy.rfc.tagDuplicate",
                            message=(
                                f"Duplicate RFC tag [{val}] — also at"
                                f" line {seen_tags[val] + 1}."
                            ),
                            line=ann.line,
                            end_line=ann.line,
                            end_character=line_len,
                            severity=lsp.DiagnosticSeverity.Warning,
                            source="ivy-lsp-semantic",
                        )
                    )
                else:
                    seen_tags[val] = ann.line

        # D6: tag gap detection (needs the fully collected file_tags)
        if len(set(file_tags)) >= 5:
            tag_set = sorted(set(file_tags))
            tag_range = tag_set[-1] - tag_set[0] + 1
            gap_count = tag_range - len(tag_set)
            gap_ratio = gap_count / tag_range if tag_range > 0 else 1.0
            if gap_ratio < 0.3:
                full_range = set(range(tag_set[0], tag_set[-1] + 1))
                missing = sorted(full_range - set(tag_set))
                for m in missing:
                    nearest_line = 0
                    for t in tag_set:
                        if abs(t - m) <= 1 and t in tag_to_line:
                            nearest_line = tag_to_line[t]
                            break
                    diags.append(
                        IvyDiagnostic(
                            code="ivy.rfc.tagGap",
                            message=f"RFC tag gap: [{m}] is missing.",
                            line=nearest_line,
                            end_line=nearest_line,
                            end_character=0,
                            severity=lsp.DiagnosticSeverity.Information,
                            source="ivy-lsp-semantic",
                        )
                    )

    # D8: Shadow declaration detection
    from ivy_lsp.core.semantic.nodes import SymbolNode

    if hasattr(model, "_nodes_by_name"):
        for name, nodes in model._nodes_by_name.items():
            sym_nodes = [n for n in nodes if isinstance(n, SymbolNode)]
            local_nodes = [n for n in sym_nodes if n.file == abs_path]
            local_proto = _protocol_from_path(abs_path)
            other_nodes = [
                n
                for n in sym_nodes
                if n.file != abs_path and _protocol_from_path(n.file) == local_proto
            ]
            if local_nodes and other_nodes:
                for local in local_nodes:
                    for other in other_nodes:
                        if local.kind == other.kind:
                            local_line = local.line
                            line_len = (
                                len(lines[local_line]) if local_line < len(lines) else 0
                            )
                            other_basename = other.file.rsplit("/", 1)[-1]
                            diags.append(
                                IvyDiagnostic(
                                    code="ivy.include.shadowDeclaration",
                                    message=(
                                        f"'{name}' shadows a declaration in"
                                        f" '{other_basename}'"
                                        f" (line {other.line + 1})."
                                    ),
                                    line=local_line,
                                    end_line=local_line,
                                    end_character=line_len,
                                    severity=lsp.DiagnosticSeverity.Hint,
                                    source="ivy-lsp-semantic",
                                    related=[
                                        RelatedLocation(
                                            file=other.file,
                                            line=other.line,
                                            message=f"original declaration of '{name}'",
                                        )
                                    ],
                                )
                            )
                            break  # one shadow warning per local symbol

    # Missing tags on assertions (Hint)
    for m in _ASSERTION_RE.finditer(source):
        line_no = source[: m.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""
        if not _TAG_RE.search(line_text):
            diags.append(
                IvyDiagnostic(
                    code="ivy.rfc.missingBracketTag",
                    message="Assertion without RFC bracket tag annotation",
                    line=line_no,
                    end_line=line_no,
                    end_character=len(line_text),
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
    from ivy_lsp.lsp.diagnostics.publisher import _convert_error_to_diagnostic

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
                diags.append(
                    IvyDiagnostic(
                        code="ivy.syntax.lexerError",
                        message=f"Lexer error: {err_msg}",
                        line=err_line,
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lsp",
                    ).to_lsp()
                )

    # Requirement analysis diagnostics
    req_diags = compute_requirement_diagnostics(source, filepath, indexer)
    diags.extend(d.to_lsp() for d in req_diags)

    # Semantic model diagnostics
    sem_diags = compute_semantic_diagnostics(semantic_model, filepath, source)
    diags.extend(d.to_lsp() for d in sem_diags)

    # Coverage hint diagnostics (C1)
    if indexer is not None:
        graph = getattr(indexer, "requirement_graph", None)
        if graph is not None:
            from ivy_lsp.core.coverage_hints import compute_coverage_hints

            lines = source.split("\n")
            for hint in compute_coverage_hints(graph, filepath):
                lsp_diag = hint.to_lsp()
                line = hint.line
                line_len = len(lines[line]) if line < len(lines) else 0
                # Expand the range to the full line (IR stores column; coverage
                # hints are more useful when highlighted end-to-end).
                lsp_diag.range = lsp.Range(
                    start=lsp.Position(line=line, character=0),
                    end=lsp.Position(line=line, character=line_len),
                )
                diags.append(lsp_diag)

    # --- Pattern diagnostics (cheap regex checks) ---
    try:
        basename = os.path.basename(filepath)

        # Check for missing _finalize in test files
        if "test" in basename.lower() and "_finalize" not in source:
            # Look for export action declarations
            has_export = bool(re.search(r"^\s*export\s+action", source, re.MULTILINE))
            if has_export:
                diags.append(
                    IvyDiagnostic(
                        code="ivy.action.missingFinalize",
                        message=(
                            "Test file has exports but no _finalize action. "
                            "Consider adding 'export action _finalize' for end-of-test assertions."
                        ),
                        line=0,
                        severity=lsp.DiagnosticSeverity.Warning,
                        source="ivy-semantic",
                    ).to_lsp()
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
                        IvyDiagnostic(
                            code="ivy.action.noMonitor",
                            message=f"Exported action '{exp_action}' has no before/after monitor in this file.",
                            line=line_num,
                            severity=lsp.DiagnosticSeverity.Hint,
                            source="ivy-semantic",
                        ).to_lsp()
                    )
    except ValueError:
        raise
    except (re.error, AttributeError, TypeError) as exc:
        # Narrow handler covers the regex / None-match / type-coercion
        # failure modes the pattern checks above could realistically hit.
        # Promoted from logger.debug to logger.warning so MCP consumers
        # see "pattern check crashed" at the default log level instead
        # of having to opt into debug. Unknown exception classes
        # propagate so they aren't masked by a too-broad handler.
        logger.warning("pattern check failed: %s", exc, exc_info=True)

    return diags


def parse_ivy_check_output(output: str) -> List[IvyDiagnostic]:
    """Parse ivy_check stderr/stdout into IvyDiagnostic IR.

    Uses ``parse_ivy_output`` directly (not ``parse_ivy_check_lines``) so
    the ``source`` field is available for granular code selection:
    - ``cpp_compiler`` source -> ``ivy.verify.compileError``
    - error severity         -> ``ivy.verify.checkError``
    - warning severity       -> ``ivy.verify.checkWarning``

    Callers at LSP boundaries must call ``.to_lsp()`` on each result.
    """
    from ivy_lsp.infra.utils.ivy_output import parse_ivy_output

    diags: List[IvyDiagnostic] = []
    for entry in parse_ivy_output(output):
        lineno = max(0, entry["line"] - 1)
        severity_str = entry["severity"]
        source_str = entry.get("source", "ivy_check")
        severity = (
            lsp.DiagnosticSeverity.Error
            if severity_str == "error"
            else lsp.DiagnosticSeverity.Warning
        )
        code = _IVY_CHECK_CODE_MAP.get(
            (source_str, severity_str),
            (
                "ivy.verify.checkError"
                if severity_str == "error"
                else "ivy.verify.checkWarning"
            ),
        )
        diags.append(
            IvyDiagnostic(
                code=code,
                message=entry["message"],
                line=lineno,
                end_line=lineno + 1,
                character=0,
                end_character=0,
                severity=severity,
                source="ivy_check",
            )
        )
    return diags


async def run_deep_diagnostics(
    filepath: str,
    ivy_check_cmd: str = "ivy_check",
    cwd: Optional[str] = None,
    timeout: float = 30.0,
) -> List[lsp.Diagnostic]:
    """Run ivy_check as subprocess and convert output to diagnostics.

    Routes through ``run_ivy_subprocess`` for semaphore enforcement,
    PID tracking, and process-group kill on timeout.
    """
    from ivy_lsp.infra.utils.async_subprocess import run_ivy_subprocess

    try:
        result = await run_ivy_subprocess(
            [ivy_check_cmd, filepath],
            timeout=timeout,
            cwd=cwd,
            use_semaphore=True,
        )
    except Exception:
        logger.warning("Deep diagnostics failed for %s", filepath, exc_info=True)
        return []

    if not result.success:
        if "not found on PATH" in result.message:
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
        if "Timed out" in result.message:
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

    output = "\n".join(result.output_lines)
    return [d.to_lsp() for d in parse_ivy_check_output(output)]
