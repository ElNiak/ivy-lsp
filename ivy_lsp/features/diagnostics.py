"""Diagnostics feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel

logger = logging.getLogger(__name__)

DEBOUNCE_DELAY = 0.5  # seconds


def _convert_error_to_diagnostic(error: Any, source: str) -> lsp.Diagnostic:
    """Convert a single Ivy parse error to an LSP Diagnostic."""
    line = 0
    message = str(error)

    if hasattr(error, "lineno"):
        lineno = error.lineno
        if hasattr(lineno, "line") and isinstance(lineno.line, int) and lineno.line > 0:
            line = lineno.line - 1

    if hasattr(error, "msg"):
        message = error.msg

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
    diags: List[lsp.Diagnostic] = []
    lines = source.split("\n")

    # 1. Missing #lang header
    stripped = source.lstrip()
    if not stripped.startswith("#lang"):
        first_len = len(lines[0]) if lines else 0
        diags.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(0, 0),
                    end=lsp.Position(0, first_len),
                ),
                message="Missing '#lang ivy1.7' header",
                severity=lsp.DiagnosticSeverity.Warning,
                source="ivy-lsp",
                code="missing-lang-header",
            )
        )

    # 2. Unmatched braces
    depth = 0
    for i, line_text in enumerate(lines):
        # Skip comments but preserve #lang lines
        if line_text.strip().startswith("#lang"):
            code = line_text
        else:
            code = line_text.split("#")[0]
        for ch in code:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth < 0:
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(i, 0),
                            end=lsp.Position(i, len(line_text)),
                        ),
                        message="Unmatched closing brace",
                        severity=lsp.DiagnosticSeverity.Error,
                        source="ivy-lsp",
                    )
                )
                depth = 0
    if depth > 0:
        last = len(lines) - 1
        last_len = len(lines[last]) if lines else 0
        diags.append(
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(last, 0),
                    end=lsp.Position(last, last_len),
                ),
                message=f"Unmatched opening brace ({depth} unclosed)",
                severity=lsp.DiagnosticSeverity.Error,
                source="ivy-lsp",
            )
        )

    # 3. Unresolved includes
    if indexer:
        for match in re.finditer(r"^include\s+(\w+)", source, re.MULTILINE):
            inc_name = match.group(1)
            resolved = indexer._resolver.resolve(inc_name, filepath)
            if resolved is None:
                line_no = source[: match.start()].count("\n")
                line_text = lines[line_no] if line_no < len(lines) else ""
                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line_no, 0),
                            end=lsp.Position(line_no, len(line_text)),
                        ),
                        message=f"Unresolved include: {inc_name}",
                        severity=lsp.DiagnosticSeverity.Warning,
                        source="ivy-lsp",
                        code="unresolved-include",
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

    graph = getattr(indexer, "_requirement_graph", None)
    include_graph = getattr(indexer, "_include_graph", None)
    if graph is None:
        return []

    import os

    diags: List[lsp.Diagnostic] = []
    abs_path = os.path.abspath(filepath)
    lines = source.split("\n")

    # 1. Include chain propagation
    if include_graph:
        for match in re.finditer(r"^include\s+(\w+)", source, re.MULTILINE):
            inc_name = match.group(1)
            line_no = source[: match.start()].count("\n")
            line_text = lines[line_no] if line_no < len(lines) else ""

            # Count requirements brought in via this include chain
            active = graph.get_active_requirements_for_file(
                abs_path, include_graph
            )
            own = graph.get_all_requirements_in_file(abs_path)
            inherited = len(active) - len(own)

            if inherited > 0:
                related = []
                for req in active:
                    if req.file != abs_path:
                        related.append(
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
                        )

                diags.append(
                    lsp.Diagnostic(
                        range=lsp.Range(
                            start=lsp.Position(line_no, 0),
                            end=lsp.Position(line_no, len(line_text)),
                        ),
                        message=(
                            f"Brings {inherited} requirements into scope "
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

    for match in re.finditer(
        r"^\s*action\s+([\w.]+)", source, re.MULTILINE
    ):
        action_name = match.group(1)
        line_no = source[: match.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""

        if active_scope is not None:
            if not active_scope.is_action_exported(action_name):
                continue
            scoped_counts = graph.get_scoped_counts(
                active_scope.test_file, action_name
            )
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
    for match in re.finditer(
        r"^\s*relation\s+([\w.]+)", source, re.MULTILINE
    ):
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
                if tag not in req_ids:
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
    req_re = re.compile(
        r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
    )
    tag_re = re.compile(r"#\s*\[")
    for m in req_re.finditer(source):
        line_no = source[: m.start()].count("\n")
        line_text = lines[line_no] if line_no < len(lines) else ""
        if not tag_re.search(line_text):
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

    result = parse_result if parse_result is not None else (
        parser.parse(source, filepath) if parser is not None else None
    )
    if result is None:
        return diags
    if not result.success:
        for error in result.errors:
            diags.append(_convert_error_to_diagnostic(error, source))

        # Surface fallback scanner lexer errors as diagnostics.
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

    return diags


def parse_ivy_check_output(output: str) -> List[lsp.Diagnostic]:
    """Parse ivy_check stderr/stdout into LSP diagnostics.

    Looks for lines matching: filename:LINE: error|warning: message
    """
    diags: List[lsp.Diagnostic] = []
    for line in output.splitlines():
        m = re.match(r".*?:(\d+):\s*(error|warning):\s*(.*)", line)
        if m:
            lineno = max(0, int(m.group(1)) - 1)
            severity = (
                lsp.DiagnosticSeverity.Error
                if m.group(2) == "error"
                else lsp.DiagnosticSeverity.Warning
            )
            diags.append(
                lsp.Diagnostic(
                    range=lsp.Range(
                        start=lsp.Position(lineno, 0),
                        end=lsp.Position(lineno, 999),
                    ),
                    message=m.group(3),
                    severity=severity,
                    source="ivy_check",
                )
            )
    return diags


async def run_deep_diagnostics(
    filepath: str,
    ivy_check_cmd: str = "ivy_check",
) -> List[lsp.Diagnostic]:
    """Run ivy_check as subprocess and convert output to diagnostics."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ivy_check_cmd,
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except FileNotFoundError:
        logger.info("%s not found on PATH", ivy_check_cmd)
        return []
    except asyncio.TimeoutError:
        logger.warning("Deep diagnostics timed out for %s", filepath)
        return []
    except Exception:
        logger.warning("Deep diagnostics failed for %s", filepath, exc_info=True)
        return []

    output = stderr.decode("utf-8", errors="replace") + stdout.decode(
        "utf-8", errors="replace"
    )
    return parse_ivy_check_output(output)


def register(server) -> None:
    """Register diagnostic handlers for didOpen, didChange, didSave."""
    _debounce_tasks: Dict[str, asyncio.Task] = {}

    def _get_semantic_model():
        return getattr(server, "_semantic_model", None)

    def _run_pipeline(source: str, filepath: str, trigger: str) -> Any:
        """Run the analysis pipeline. Returns the ParseResult (or None)."""
        pipeline = getattr(server, "_analysis_pipeline", None)
        if pipeline:
            try:
                return pipeline.analyze(source, filepath, trigger)
            except Exception:
                logger.debug("Pipeline analysis failed for %s", filepath, exc_info=True)
        return None

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    def did_open(params: lsp.DidOpenTextDocumentParams) -> None:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        filepath = uri.replace("file://", "")
        source = doc.source or ""
        pipeline_result = _run_pipeline(source, filepath, "change")
        diags = compute_diagnostics(
            server._parser, source, filepath,
            server._indexer, _get_semantic_model(),
            parse_result=pipeline_result,
        )
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
        )

    @server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
    def did_change(params: lsp.DidChangeTextDocumentParams) -> None:
        uri = params.text_document.uri
        old_task = _debounce_tasks.pop(uri, None)
        if old_task and not old_task.done():
            old_task.cancel()

        async def _debounced():
            try:
                await asyncio.sleep(DEBOUNCE_DELAY)
                doc = server.workspace.get_text_document(uri)
                filepath = uri.replace("file://", "")
                diags = compute_diagnostics(
                    server._parser, doc.source or "", filepath,
                    server._indexer, _get_semantic_model(),
                )
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Debounced diagnostics failed for %s", uri, exc_info=True)

        loop = asyncio.get_running_loop()
        task = loop.create_task(_debounced())
        task.add_done_callback(
            lambda t, u=uri: _debounce_tasks.pop(u, None)
            if _debounce_tasks.get(u) is t
            else None
        )
        _debounce_tasks[uri] = task

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def did_save(params: lsp.DidSaveTextDocumentParams) -> None:
        uri = params.text_document.uri
        filepath = uri.replace("file://", "")
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""
        pipeline_result = _run_pipeline(source, filepath, "save")
        diags = compute_diagnostics(
            server._parser, source, filepath,
            server._indexer, _get_semantic_model(),
            parse_result=pipeline_result,
        )
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags)
        )

        if filepath.endswith(".ivy") and server._indexer is not None:
            server._indexer.reindex_file_with_dependents(filepath)

        async def _deep():
            try:
                deep = await run_deep_diagnostics(filepath)
                server.text_document_publish_diagnostics(
                    lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diags + deep)
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("Deep diagnostics task failed for %s", uri, exc_info=True)

        loop = asyncio.get_running_loop()
        loop.create_task(_deep())
