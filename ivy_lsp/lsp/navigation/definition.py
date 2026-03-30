"""Go-to-definition feature for Ivy LSP."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Union

from lsprotocol import types as lsp

from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.infra.utils.position_utils import make_range, word_at_position
from ivy_lsp.infra.utils.symbol_resolver import (
    ensure_deep_parsed,
    lookup_with_dotted_fallback,
)

logger = logging.getLogger(__name__)


_INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)")
_DECL_RE = re.compile(
    r"^\s*(?:action|relation|function|individual|type|module|object|isolate)\s+(\w+)"
)


def goto_definition(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    semantic_model: Any = None,
) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
    """Find definition(s) of the symbol at the given position.

    Extracts the word under the cursor from *source_lines* at *position*,
    then queries the *indexer* for matching symbol definitions across the
    workspace.

    If the word is a dotted name (e.g. ``frame.ack``) and no results are
    found for the full qualified name, falls back to looking up just the
    last component (``ack``).

    Args:
        indexer: A :class:`WorkspaceIndexer` instance to query.
        filepath: Absolute path to the file being edited.
        position: The cursor position (0-based line and character).
        source_lines: The source file split into lines.
        semantic_model: Optional SemanticModel for fallback lookups.

    Returns:
        A single :class:`lsp.Location` when exactly one definition is found,
        a list of locations when multiple definitions match,
        or ``None`` when no definition can be located.
    """
    # Demand-driven deep parse for shared modules
    ensure_deep_parsed(indexer, filepath)

    # H5: Check if cursor is on an include line
    if position.line < len(source_lines):
        line_text = source_lines[position.line]
        inc_match = _INCLUDE_RE.match(line_text)
        if inc_match:
            include_name = inc_match.group(1)
            col_start = line_text.index(include_name)
            col_end = col_start + len(include_name)
            if col_start <= position.character < col_end:
                resolved = indexer.resolver.resolve(include_name, filepath)
                if resolved:
                    uri = Path(resolved).as_uri()
                    r = lsp.Range(
                        start=lsp.Position(line=0, character=0),
                        end=lsp.Position(line=0, character=0),
                    )
                    return lsp.Location(uri=uri, range=r)
                return None

    word = word_at_position(source_lines, position)
    if not word:
        return None

    results = lookup_with_dotted_fallback(indexer, word)

    if not results and semantic_model is not None:
        results = _lookup_via_semantic_model(word, semantic_model)

    if not results:
        # H6: If cursor is on a declaration keyword, return self-location
        if position.line < len(source_lines):
            line_text = source_lines[position.line]
            decl_match = _DECL_RE.match(line_text)
            if decl_match and decl_match.group(1) == word.split(".")[-1]:
                uri = Path(filepath).as_uri()
                col = line_text.index(decl_match.group(1))
                r = lsp.Range(
                    start=lsp.Position(line=position.line, character=col),
                    end=lsp.Position(
                        line=position.line, character=col + len(decl_match.group(1))
                    ),
                )
                return lsp.Location(uri=uri, range=r)
        return None

    # Rank results by scope relevance: files in the same endpoint mirror
    # scope as the requesting file rank first.
    if len(results) > 1 and hasattr(indexer, "get_scope_files_for_file"):
        scope_files = indexer.get_scope_files_for_file(filepath)
        resolver = getattr(indexer, "resolver", None)
        results = _rank_by_scope(results, filepath, scope_files, resolver=resolver)

    locations = []
    for sl in results:
        uri = Path(sl.filepath).as_uri() if sl.filepath else ""
        r = make_range(*sl.range)
        locations.append(lsp.Location(uri=uri, range=r))

    if len(locations) == 1:
        return locations[0]
    return locations


def _rank_by_scope(
    results: list, current_filepath: str, scope_files: set, resolver=None
) -> list:
    """Rank definition results by scope relevance.

    Delegates to the shared :func:`ivy_lsp.infra.utils.scope_ranking.rank_by_scope`.
    """
    from ivy_lsp.infra.utils.scope_ranking import rank_by_scope

    return rank_by_scope(results, current_filepath, scope_files, resolver=resolver)


class _SemanticSymbolLoc:
    """Lightweight stand-in for an indexer symbol result."""

    __slots__ = ("filepath", "range")

    def __init__(self, filepath: str, line: int):
        self.filepath = filepath
        self.range = (line - 1, 0, line - 1, 0)


def _lookup_via_semantic_model(word: str, semantic_model: Any) -> list:
    """Query the SemanticModel for symbol locations using O(1) name index."""
    try:
        results = []
        for node in semantic_model.get_nodes_by_name(word):
            if getattr(node, "file", None) and getattr(node, "line", None):
                results.append(_SemanticSymbolLoc(node.file, node.line))
        if not results and "." in word:
            # Fallback: qualified name — try last segment
            last = word.rsplit(".", 1)[-1]
            for node in semantic_model.get_nodes_by_name(last):
                qn = getattr(node, "qualified_name", None)
                if qn == word and node.file and node.line:
                    results.append(_SemanticSymbolLoc(node.file, node.line))
        return results
    except Exception:
        logger.debug("semantic model lookup failed", exc_info=True)
        return []


def register(server) -> None:
    """Register the ``textDocument/definition`` feature handler."""
    from ivy_lsp.lsp.navigation._handler import run_navigation_handler

    @server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    async def definition(
        params: lsp.DefinitionParams,
    ) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
        """Handle textDocument/definition requests."""
        result = await run_navigation_handler(
            params,
            server,
            lambda ctx: goto_definition(
                ctx.indexer,
                ctx.filepath,
                ctx.position,
                ctx.lines,
                semantic_model=ctx.model,
            ),
            track_active_uri=True,
            trace_method="textDocument/definition",
        )
        try:
            from ivy_lsp.infra.observability import get_tracer

            tracer = get_tracer()
            if tracer is not None:
                lines = server.workspace.get_text_document(
                    params.text_document.uri
                ).source.split("\n")
                word = word_at_position(lines, params.position) if lines else None
                loc_count = (
                    len(result)
                    if isinstance(result, list)
                    else (1 if result is not None else 0)
                )
                tracer.trace_lsp_request(
                    method="textDocument/definition",
                    filepath=uri_to_path(params.text_document.uri),
                    position=f"{params.position.line}:{params.position.character}",
                    word=word,
                    result_summary=(f"{loc_count} location(s)" if result else None),
                )
        except Exception:
            pass
        return result
