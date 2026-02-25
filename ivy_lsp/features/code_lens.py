"""Code lens provider for Ivy requirement analysis.

Displays inline annotations above monitor blocks, state variable declarations,
property/axiom/invariant declarations, and include directives showing
requirement counts, state variable reads, and cross-file propagation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, List

from lsprotocol import types as lsp

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
) -> List[lsp.CodeLens]:
    """Compute all code lenses for a file."""
    graph = getattr(indexer, "_requirement_graph", None)
    if graph is None:
        return []

    abs_path = os.path.abspath(filepath)
    lenses: List[lsp.CodeLens] = []
    lines = source.split("\n")

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

    return lenses


def _monitor_lenses(
    lines: List[str],
    filepath: str,
    graph: Any,
) -> List[lsp.CodeLens]:
    """Code lenses above before/after/around lines."""
    lenses = []
    source = "\n".join(lines)

    for m in _MONITOR_LINE_RE.finditer(source):
        action_name = m.group(2)
        line = source[: m.start()].count("\n")

        reqs = graph.get_requirements_for_action(action_name)
        if not reqs:
            continue

        counts = graph.get_requirement_counts_for_action(action_name)

        # Count state vars read
        var_ids: set = set()
        for req in reqs:
            for etype, target in graph._outgoing.get(req.id, []):
                from ivy_lsp.analysis.requirement_graph import EdgeType

                if etype == EdgeType.READS:
                    var_ids.add(target)

        parts = []
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
                    command="",
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
                        command="",
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


def register(server) -> None:
    """Register the code lens handler."""

    @server.feature(lsp.TEXT_DOCUMENT_CODE_LENS)
    def code_lens(params: lsp.CodeLensParams) -> List[lsp.CodeLens]:
        uri = params.text_document.uri
        filepath = uri.replace("file://", "")
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""

        if not server._indexer:
            return []

        try:
            return compute_code_lenses(server._indexer, filepath, source)
        except Exception:
            logger.warning(
                "Code lens computation failed for %s",
                filepath,
                exc_info=True,
            )
            return []
