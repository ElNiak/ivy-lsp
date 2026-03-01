"""Code lens provider for Ivy requirement and RFC traceability analysis.

Displays inline annotations above monitor blocks, state variable declarations,
property/axiom/invariant declarations, include directives, and RFC bracket tags
showing requirement counts, state variable reads, cross-file propagation,
and workspace-level RFC coverage summaries.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, List

from lsprotocol import types as lsp

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
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
_INCLUDE_LINE_RE = re.compile(
    r"^\s*include\s+(\w+)", re.MULTILINE
)


def compute_code_lenses(
    indexer: Any,
    filepath: str,
    source: str,
    semantic_model: Any = None,
) -> List[lsp.CodeLens]:
    """Compute all code lenses for a file."""
    graph = getattr(indexer, "_requirement_graph", None)
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
        include_graph = getattr(indexer, "_include_graph", None)
        if include_graph:
            lenses.extend(_include_lenses(lines, abs_path, graph, include_graph))

    # 5. Semantic model lenses (RFC tags, coverage summary)
    if semantic_model is not None:
        lenses.extend(_rfc_tag_lenses(lines, abs_path, semantic_model))
        lenses.extend(_coverage_summary_lens(abs_path, semantic_model))

    return lenses


def _monitor_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above before/after/around lines."""
    lenses = []
    source = "\n".join(lines)

    # Determine active scope once (immutable during iteration)
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
            # NCT path: covers both exported (GUARANTEE) and imported (ASSUMPTION)
            counts = {e["kind"]: e["count"] for e in nct_entries}
            reqs = [
                r
                for r in graph.get_scoped_requirements(active_scope.test_file)
                if r.monitor_action == action_name
            ]
        elif active_scope is not None:
            # Scoped, no NCT entries (internal action) — try regular scoped counts
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

        # Count state vars read
        var_ids: set = set()
        for req in reqs:
            for etype, target in graph._outgoing.get(req.id, []):
                from ivy_lsp.analysis.requirement_graph import EdgeType

                if etype == EdgeType.READS:
                    var_ids.add(target)

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

        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=line, character=0),
                    end=lsp.Position(line=line, character=len(lines[line]) if line < len(lines) else 0),
                ),
                command=lsp.Command(
                    title=title,
                    command="ivy.showActionRequirements",
                    arguments=[action_name],
                ),
            )
        )

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

            lenses.append(
                lsp.CodeLens(
                    range=lsp.Range(
                        start=lsp.Position(line=line, character=0),
                        end=lsp.Position(
                            line=line,
                            character=len(lines[line]) if line < len(lines) else 0,
                        ),
                    ),
                    command=lsp.Command(
                        title=title,
                        command="ivy.showActionRequirements",
                        arguments=[var_name],
                    ),
                )
            )

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
        prop_node = None
        for p in graph.properties.values():
            if p.file == filepath and p.line == line:
                prop_node = p
                break

        if prop_node is None:
            continue

        # Find shared state vars with requirements
        from ivy_lsp.analysis.requirement_graph import EdgeType

        var_ids = {
            target
            for etype, target in graph._outgoing.get(prop_node.id, [])
            if etype == EdgeType.READS
        }

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

        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=line, character=0),
                    end=lsp.Position(
                        line=line,
                        character=len(lines[line]) if line < len(lines) else 0,
                    ),
                ),
                command=lsp.Command(
                    title=title,
                    command="",
                ),
            )
        )

    return lenses


def _include_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
    include_graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above include directives."""
    lenses = []
    source = "\n".join(lines)

    for m in _INCLUDE_LINE_RE.finditer(source):
        line = source[: m.start()].count("\n")

        # Count requirements brought into scope via this include chain
        active_reqs = graph.get_active_requirements_for_file(filepath, include_graph)
        own_reqs = graph.get_all_requirements_in_file(filepath)
        inherited_count = len(active_reqs) - len(own_reqs)

        if inherited_count <= 0:
            continue

        title = f"brings {inherited_count} requirements into scope"

        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=line, character=0),
                    end=lsp.Position(
                        line=line,
                        character=len(lines[line]) if line < len(lines) else 0,
                    ),
                ),
                command=lsp.Command(
                    title=title,
                    command="",
                ),
            )
        )

    return lenses


def _rfc_tag_lenses(
    lines: List[str],
    filepath: str,
    semantic_model: Any,
) -> List[lsp.CodeLens]:
    """Code lenses showing RFC bracket tags on monitor lines."""
    from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

    lenses = []
    abs_path = os.path.abspath(filepath)
    annotations = [
        n
        for n in semantic_model.get_nodes_by_type(RfcAnnotation)
        if n.file
        and (n.file == abs_path or n.file == filepath or os.path.abspath(n.file) == abs_path)
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
        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=line, character=0),
                    end=lsp.Position(
                        line=line,
                        character=len(lines[line]) if line < len(lines) else 0,
                    ),
                ),
                command=lsp.Command(title=title, command=""),
            )
        )

    return lenses


def _coverage_summary_lens(
    filepath: str,
    semantic_model: Any,
) -> List[lsp.CodeLens]:
    """File-level coverage summary lens at line 0."""
    from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

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
            command=lsp.Command(title=title, command=""),
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

        if not server._indexer:
            return []

        rfc_coverage = getattr(server, "_rfc_coverage_enabled", True)
        model = getattr(server, "_semantic_model", None)
        if not rfc_coverage:
            model = None
        try:
            return compute_code_lenses(
                server._indexer, filepath, source, model
            )
        except Exception:
            logger.warning(
                "Code lens computation failed for %s",
                filepath,
                exc_info=True,
            )
            return []
