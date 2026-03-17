"""Go-to-definition feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Union

from lsprotocol import types as lsp

from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import make_range, word_at_position

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

    results = indexer.lookup_symbol(word)
    if not results and "." in word:
        last = word.rsplit(".", 1)[1]
        results = indexer.lookup_symbol(last)

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
        results = _rank_by_scope(results, filepath, scope_files)

    locations = []
    for sl in results:
        uri = Path(sl.filepath).as_uri() if sl.filepath else ""
        r = make_range(*sl.range)
        locations.append(lsp.Location(uri=uri, range=r))

    if len(locations) == 1:
        return locations[0]
    return locations


def _rank_by_scope(results: list, current_filepath: str, scope_files: set) -> list:
    """Rank definition results by scope relevance.

    Files in the same include closure as *current_filepath* rank first,
    then same-directory, then by common path length.
    """
    import os

    current_norm = os.path.normpath(os.path.abspath(current_filepath))
    current_dir = os.path.dirname(current_norm)

    def _score(r):
        rpath = os.path.normpath(os.path.abspath(getattr(r, "filepath", "") or ""))
        in_scope = rpath in scope_files
        if rpath == current_norm:
            return (0, 0)
        if in_scope and os.path.dirname(rpath) == current_dir:
            return (1, 0)
        if in_scope:
            return (2, 0)
        if os.path.dirname(rpath) == current_dir:
            return (3, 0)
        return (4, 0)

    return sorted(results, key=_score)


class _SemanticSymbolLoc:
    """Lightweight stand-in for an indexer symbol result."""

    __slots__ = ("filepath", "range")

    def __init__(self, filepath: str, line: int):
        self.filepath = filepath
        self.range = (line - 1, 0, line - 1, 0)


def _lookup_via_semantic_model(word: str, semantic_model: Any) -> list:
    """Query the SemanticModel for symbol locations when the indexer misses.

    Returns a list of ``_SemanticSymbolLoc`` objects (matching the shape
    expected by the calling code in ``goto_definition``).
    """
    # TODO: add a by-name index to SemanticModel to avoid O(N) scans here
    # and in hover.py (_enrich_with_semantic_model).
    try:
        from ivy_lsp.semantic.nodes import SymbolNode, TypeNode

        results = []
        for node_type in (SymbolNode, TypeNode):
            for node in semantic_model.get_nodes_by_type(node_type):
                if node.name == word or getattr(node, "qualified_name", None) == word:
                    if node.file and node.line:
                        results.append(_SemanticSymbolLoc(node.file, node.line))
        return results
    except Exception:
        logger.debug("semantic model lookup failed", exc_info=True)
        return []


def register(server) -> None:
    """Register the ``textDocument/definition`` feature handler.

    Args:
        server: The pygls ``LanguageServer`` instance to register on.
    """

    @server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    async def definition(
        params: lsp.DefinitionParams,
    ) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
        """Handle textDocument/definition requests."""
        try:
            uri = params.text_document.uri
            doc = server.workspace.get_text_document(uri)
            if server.indexer is None:
                return None
            lines = doc.source.split("\n") if doc.source else []
            filepath = uri_to_path(uri)
            model = server.semantic_model
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: goto_definition(
                    server.indexer,
                    filepath,
                    params.position,
                    lines,
                    semantic_model=model,
                ),
            )
        except Exception:
            logger.warning("definition handler failed", exc_info=True)
            return None
