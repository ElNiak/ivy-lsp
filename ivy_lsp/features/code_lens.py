"""Code lens provider for Ivy requirement and RFC traceability analysis.

Provides six lens types:
1. Monitor lenses -- requirement counts above before/after/around blocks
2. State variable lenses -- reader counts above relation/function/individual
3. Property lenses -- shared-state analysis above invariant/axiom/conjecture
4. Include lenses -- uniquely-scoped requirement counts above include directives
5. RFC tag lenses -- bracket-tag annotations on annotated source lines
6. Coverage summary -- workspace-wide RFC coverage at line 0
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, List

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.parsing.fallback_scanner import fallback_scan
from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement
from ivy_lsp.utils import uri_to_path

logger = logging.getLogger(__name__)

# Patterns for detecting monitor/declaration lines in source
_MONITOR_LINE_RE = re.compile(
    r"^\s*(before|after|around)\s+([\w.]+)", re.MULTILINE
)
_RELATION_LINE_RE = re.compile(
    r"^\s*relation\s+([\w.]+)", re.MULTILINE
)
_FUNCTION_LINE_RE = re.compile(
    r"^\s*function\s+([\w.]+)", re.MULTILINE
)
_INDIVIDUAL_LINE_RE = re.compile(
    r"^\s*individual\s+([\w.]+)", re.MULTILINE
)
_PROPERTY_LINE_RE = re.compile(
    r"^\s*(invariant|property|axiom|conjecture)\s+", re.MULTILINE
)


def _make_lens(
    lines: List[str],
    line: int,
    title: str,
    command: str,
    arguments: List[Any] | None = None,
) -> lsp.CodeLens:
    """Build a CodeLens at the given source line."""
    end_char = len(lines[line]) if line < len(lines) else 0
    return lsp.CodeLens(
        range=lsp.Range(
            start=lsp.Position(line=line, character=0),
            end=lsp.Position(line=line, character=end_char),
        ),
        command=lsp.Command(
            title=title,
            command=command,
            arguments=arguments or [],
        ),
    )


def compute_code_lenses(
    indexer: Any,
    filepath: str,
    source: str,
    semantic_model: Any = None,
) -> List[lsp.CodeLens]:
    """Compute all code lenses for a file."""
    if indexer is None:
        return []
    graph = getattr(indexer, "requirement_graph", None)
    abs_path = os.path.abspath(filepath)
    lenses: List[lsp.CodeLens] = []
    lines = source.split("\n")

    if graph is not None:
        # 1. Monitor block lenses (before/after/around)
        lenses.extend(_monitor_lenses(lines, abs_path, graph))

        # 2. State variable lenses (relation/function/individual)
        lenses.extend(_state_var_lenses(lines, abs_path, graph))

        # 3. Property/axiom/conjecture lenses
        lenses.extend(_property_lenses(lines, abs_path, graph))

        # 4. Include directive lenses
        include_graph = indexer.include_graph
        if include_graph:
            resolver = indexer.resolver
            lenses.extend(
                _include_lenses(lines, abs_path, graph, include_graph, resolver)
            )

    # 5. Semantic model lenses (RFC tags, coverage summary)
    if semantic_model is not None:
        lenses.extend(_rfc_tag_lenses(lines, abs_path, semantic_model))
        lenses.extend(_coverage_summary_lens(semantic_model))

    return lenses


def _monitor_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above before/after/around monitor lines.

    Three retrieval paths depending on graph capabilities:
    1. NCT-scoped: per-requirement NCT tags (GUARANTEE/ASSUMPTION/TESTER_ONLY)
    2. Scoped (non-NCT): kind-based counts for exported actions only
    3. Unscoped: graph-wide requirement counts

    Each lens also reports the number of state variables read.
    """
    lenses = []
    source = "\n".join(lines)

    # Determine active scope once
    active_scope = None
    if isinstance(graph, ScopedRequirementModel):
        active_scope = graph.get_active_scope()

    for m in _MONITOR_LINE_RE.finditer(source):
        action_name = m.group(2)
        line = source[: m.start()].count("\n")

        # --- NCT-aware count retrieval ---
        nct_entries = None
        if active_scope is not None:
            nct_entries = graph.get_scoped_nct_counts(
                active_scope.test_file, action_name
            )

        if nct_entries:
            # NCT path: per-requirement classification (GUARANTEE/ASSUMPTION/TESTER_ONLY)
            counts = {e["kind"]: e["count"] for e in nct_entries}
            reqs = [
                r
                for r in graph.get_scoped_requirements(active_scope.test_file)
                if r.monitor_action == action_name
            ]
        elif active_scope is not None:
            # Scoped but no NCT entries -- fall back to regular scoped counts
            counts = graph.get_scoped_counts(active_scope.test_file, action_name)
            if not counts:
                continue
            reqs = [
                r
                for r in graph.get_scoped_requirements(active_scope.test_file)
                if r.monitor_action == action_name
            ]
        else:
            # Unscoped: original behavior
            reqs = graph.get_requirements_for_action(action_name)
            if not reqs:
                continue
            counts = graph.get_requirement_counts_for_action(action_name)

        if not counts:
            continue

        # Count state vars read (uses lock-protected public API)
        var_ids: set = set()
        for req in reqs:
            for sv in graph.get_state_vars_read_by(req.id):
                var_ids.add(sv.id)

        # --- Title building ---
        parts = []
        if nct_entries:
            for entry in nct_entries:
                parts.append(
                    f"{entry['count']} {entry['kind']} [{entry['nct_tag']}]"
                )
        else:
            for kind in ("require", "ensure", "assume", "assert"):
                if kind in counts:
                    parts.append(f"{counts[kind]} {kind}")
        if var_ids:
            parts.append(f"reads {len(var_ids)} state vars")

        title = " | ".join(parts) if parts else f"{len(reqs)} requirements"

        lenses.append(_make_lens(lines, line, title, "ivy.showActionRequirements", [action_name]))

    return lenses


def _state_var_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above relation/function/individual declarations."""
    lenses = []
    source = "\n".join(lines)

    for pattern in (_RELATION_LINE_RE, _FUNCTION_LINE_RE, _INDIVIDUAL_LINE_RE):
        for m in pattern.finditer(source):
            var_name = m.group(1)
            line = source[: m.start()].count("\n")

            readers = graph.get_requirements_sharing_state_var(var_name)
            if not readers:
                continue

            files: set = {r.file for r in readers}
            title = f"read by {len(readers)} requirements across {len(files)} files"

            lenses.append(_make_lens(lines, line, title, "ivy.showActionRequirements", [var_name]))

    return lenses


def _property_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above invariant/property/axiom/conjecture declarations."""
    lenses = []
    source = "\n".join(lines)

    for m in _PROPERTY_LINE_RE.finditer(source):
        line = source[: m.start()].count("\n")

        # Find property node at this line
        # NOTE: graph.properties is accessed without lock.
        # Property lens tolerates stale data; worst case is a missing lens.
        prop_node = None
        for p in graph.properties.values():
            if p.file == filepath and p.line == line:
                prop_node = p
                break

        if prop_node is None:
            continue

        # Find shared state vars with requirements (lock-protected API)
        var_ids = {sv.id for sv in graph.get_state_vars_read_by(prop_node.id)}

        shared_reqs: set = set()
        for var_id in var_ids:
            for req in graph.get_requirements_sharing_state_var(var_id):
                shared_reqs.add(req.id)

        active_files: set = set()
        for req_id in shared_reqs:
            req = graph.requirements.get(req_id)
            if req:
                active_files.add(req.file)

        parts = []
        if shared_reqs:
            parts.append(f"shares state with {len(shared_reqs)} requirements")
        if active_files:
            parts.append(f"active in {len(active_files)} files")

        if not parts:
            continue

        title = " | ".join(parts)

        lenses.append(_make_lens(lines, line, title, "ivy.showPropertyDetails", [prop_node.id]))

    return lenses


def _include_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
    include_graph: Any,
    resolver: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above include directives.

    Each include shows the number of requirements it *uniquely* brings into
    scope.  When two includes share a transitive dependency, the first one
    (in source order) claims the shared files' requirements.
    """
    lenses = []

    if resolver is None:
        return lenses

    source = "\n".join(lines)
    symbols, _ = fallback_scan(source, filepath)
    include_symbols = [
        s for s in symbols if s.kind == lsp.SymbolKind.File and s.detail == "include"
    ]

    seen_files: set = set()

    for sym in include_symbols:
        include_name = sym.name
        line = sym.range[0]  # 0-based line index

        resolved_path = resolver.resolve(include_name, filepath)
        if resolved_path is None:
            continue

        # Transitive closure: the resolved file + everything it includes
        transitive_files = include_graph.get_transitive_includes(
            resolved_path
        ) | {resolved_path}

        # Only count files not already claimed by earlier includes
        new_files = transitive_files - seen_files
        seen_files |= new_files

        # NOTE: graph.requirements dict access is not lock-protected here.
        # Acceptable because include lenses tolerate stale data.
        inherited_count = sum(
            1 for r in graph.requirements.values() if r.file in new_files
        )

        if inherited_count <= 0:
            continue

        title = f"brings {inherited_count} requirements into scope"

        lenses.append(_make_lens(lines, line, title, "ivy.navigateToInclude", [include_name]))

    return lenses


def _rfc_tag_lenses(
    lines: List[str],
    filepath: str,
    semantic_model: Any,
) -> List[lsp.CodeLens]:
    """Code lenses showing RFC bracket tags on annotated source lines."""
    lenses = []
    # filepath is already absolute from compute_code_lenses
    annotations = [
        n
        for n in semantic_model.get_nodes_by_type(RfcAnnotation)
        if n.file
        and (n.file == filepath or os.path.abspath(n.file) == filepath)
    ]

    for ann in annotations:
        line = ann.line
        if line < 0 or line >= len(lines):
            continue

        tag_parts = []
        for tag in ann.tags:
            req = semantic_model.get_node(tag)
            if req and isinstance(req, RfcRequirement):
                tag_parts.append(f"[{tag}] ({req.level})")
            else:
                tag_parts.append(f"[{tag}]")

        if not tag_parts:
            continue

        title = "RFC: " + ", ".join(tag_parts)
        lenses.append(_make_lens(
            lines, line, title, "ivy.showRfcDetails",
            [ann.tags[0] if ann.tags else ""],
        ))

    return lenses


def _coverage_summary_lens(
    semantic_model: Any,
) -> List[lsp.CodeLens]:
    """Workspace-wide RFC coverage summary lens placed at line 0."""
    requirements = semantic_model.get_nodes_by_type(RfcRequirement)
    if not requirements:
        return []

    annotations = semantic_model.get_nodes_by_type(RfcAnnotation)
    covered_tags = set()
    for ann in annotations:
        covered_tags.update(ann.tags)

    # Group requirements by RFC
    by_rfc: dict = {}
    for req in requirements:
        rfc = req.rfc
        if rfc not in by_rfc:
            by_rfc[rfc] = {"total": 0, "covered": 0}
        by_rfc[rfc]["total"] += 1
        if req.id in covered_tags:
            by_rfc[rfc]["covered"] += 1

    parts = []
    for rfc, stats in sorted(by_rfc.items()):
        parts.append(f"Workspace {rfc}: {stats['covered']}/{stats['total']} covered")

    if not parts:
        return []

    title = " | ".join(parts)
    return [
        lsp.CodeLens(
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=0, character=0),
            ),
            command=lsp.Command(title=title, command="ivy.noop"),
        )
    ]


def register(server) -> None:
    """Register the code lens handler."""

    @server.feature(lsp.TEXT_DOCUMENT_CODE_LENS)
    async def code_lens(params: lsp.CodeLensParams) -> List[lsp.CodeLens]:
        if not getattr(server, "_code_lens_enabled", True):
            return []

        uri = params.text_document.uri
        filepath = uri_to_path(uri)
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""

        if not server.indexer:
            return []

        rfc_coverage = getattr(server, "_rfc_coverage_enabled", True)
        model = server.semantic_model
        if not rfc_coverage:
            model = None
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, compute_code_lenses,
                server.indexer, filepath, source, model,
            )
        except (IndexError, KeyError, ValueError) as exc:
            logger.warning(
                "Code lens data inconsistency for %s: %s",
                filepath,
                exc,
            )
            return []
        except Exception:
            logger.error(
                "Unexpected error computing code lenses for %s",
                filepath,
                exc_info=True,
            )
            return []
