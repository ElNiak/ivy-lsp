"""Workspace symbol search for the Ivy Language Server.

Implements the ``workspace/symbol`` LSP request by flattening hierarchical
:class:`IvySymbol` trees into qualified-name :class:`FlatSymbol` lists,
supporting case-insensitive substring matching, and converting results to
LSP :class:`WorkspaceSymbol` objects.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from lsprotocol import types as lsp

from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.utils.position_utils import make_range

logger = logging.getLogger(__name__)

MAX_RESULTS = 100

# Internal limit to prevent memory issues on huge workspaces.
# Final MAX_RESULTS cap is applied after relevance sorting in
# compute_workspace_symbols().
_SEARCH_INTERNAL_LIMIT = 1000

# Definition kinds that should rank above references in search results.
_DEFINITION_KINDS = frozenset(
    {
        lsp.SymbolKind.Class,
        lsp.SymbolKind.Module,
        lsp.SymbolKind.Function,
        lsp.SymbolKind.Variable,
        lsp.SymbolKind.Namespace,
        lsp.SymbolKind.Property,
    }
)


def _def_boost(fs: "FlatSymbol", q_lower: str) -> int:
    """Return 0 if the symbol's leaf name exactly matches the query, else 1."""
    leaf = fs.qualified_name.rsplit(".", 1)[-1].lower()
    return 0 if (leaf == q_lower and fs.kind in _DEFINITION_KINDS) else 1


@dataclass
class FlatSymbol:
    """Flattened symbol with qualified name for workspace search.

    Attributes:
        qualified_name: Dot-separated path (e.g. ``"frame.ack.range"``).
        kind: LSP symbol kind.
        file_path: Originating file path, or ``None``.
        range: 0-based ``(start_line, start_col, end_line, end_col)`` span.
    """

    qualified_name: str
    kind: lsp.SymbolKind
    file_path: Optional[str]
    range: tuple  # (sl, sc, el, ec)


def flatten_symbols(symbols: List[IvySymbol], prefix: str = "") -> List[FlatSymbol]:
    """Recursively flatten IvySymbol trees into qualified-name list.

    Each symbol becomes a :class:`FlatSymbol` whose ``qualified_name`` is
    formed by joining the ancestor chain with dots.  Children are visited
    depth-first, so the parent always precedes its descendants in the
    returned list.

    Args:
        symbols: Top-level symbols to flatten.
        prefix: Dotted prefix to prepend (used in recursive calls).

    Returns:
        A flat list of :class:`FlatSymbol` instances.
    """
    result: List[FlatSymbol] = []
    for sym in symbols:
        qname = f"{prefix}.{sym.name}" if prefix else sym.name
        result.append(
            FlatSymbol(
                qualified_name=qname,
                kind=sym.kind,
                file_path=sym.file_path,
                range=sym.range,
            )
        )
        if sym.children:
            result.extend(flatten_symbols(sym.children, prefix=qname))
    return result


def search_symbols(flat: List[FlatSymbol], query: str) -> List[FlatSymbol]:
    """Case-insensitive substring search over flattened symbols.

    Returns matching symbols capped at an internal limit. The caller
    is responsible for relevance sorting and applying the final
    ``MAX_RESULTS`` cap.
    """
    if not query:
        return flat[:MAX_RESULTS]
    q = query.lower()
    matches = [s for s in flat if q in s.qualified_name.lower()]

    # Sort by relevance before truncation: exact leaf > prefix > substring
    def _relevance(fs: FlatSymbol) -> tuple:
        leaf = fs.qualified_name.rsplit(".", 1)[-1].lower()
        if leaf == q:
            return (0, fs.qualified_name)
        if leaf.startswith(q):
            return (1, fs.qualified_name)
        if fs.qualified_name.lower().startswith(q):
            return (2, fs.qualified_name)
        return (3, fs.qualified_name)

    matches.sort(key=_relevance)
    return matches[:_SEARCH_INTERNAL_LIMIT]


def to_workspace_symbol(flat: FlatSymbol) -> lsp.WorkspaceSymbol:
    """Convert a FlatSymbol to an LSP WorkspaceSymbol.

    Args:
        flat: The flattened symbol to convert.

    Returns:
        An LSP WorkspaceSymbol with a ``file://`` URI and range.
    """
    uri = Path(flat.file_path).as_uri() if flat.file_path else ""
    r = make_range(*flat.range)
    return lsp.WorkspaceSymbol(
        name=flat.qualified_name,
        kind=flat.kind,
        location=lsp.Location(uri=uri, range=r),
    )


def compute_workspace_symbols(
    indexer,
    query: str,
    active_filepath: Optional[str] = None,
) -> List[lsp.WorkspaceSymbol]:
    """Query the workspace indexer and return matching LSP WorkspaceSymbols.

    When *active_filepath* is provided and the indexer supports mirror
    scoping, results from the active file's scope are ranked first.
    """
    if indexer is None:
        return []
    all_syms = indexer.lookup_all_symbols()
    flat = flatten_symbols(all_syms)
    matches = search_symbols(flat, query)

    # Scope-aware ranking: in-scope symbols sort before out-of-scope.
    scope_files = None
    if active_filepath and hasattr(indexer, "get_scope_files_for_file"):
        scope_files = indexer.get_scope_files_for_file(active_filepath)

    if scope_files is not None:
        import os

        q_lower = query.lower()

        resolver = getattr(indexer, "resolver", None)
        current_layer = None
        if active_filepath and resolver and hasattr(resolver, "_file_to_layer"):
            current_layer = resolver._file_to_layer.get(
                os.path.normpath(os.path.abspath(active_filepath))
            )

        def _scope_sort_key(fs: FlatSymbol):
            fp = os.path.abspath(fs.file_path) if fs.file_path else ""
            scope_priority = 0 if fp in scope_files else 1
            layer_priority = 0
            if scope_priority == 1 and current_layer and resolver:
                r_layer = resolver._file_to_layer.get(os.path.normpath(fp))
                if r_layer == current_layer:
                    layer_priority = 0  # same layer
                else:
                    layer_priority = 1  # different layer or unknown
            return (
                _def_boost(fs, q_lower),
                scope_priority,
                layer_priority,
                fs.qualified_name,
            )

        matches = sorted(matches, key=_scope_sort_key)[:MAX_RESULTS]
    elif query:
        q_lower = query.lower()
        matches = sorted(
            matches, key=lambda fs: (_def_boost(fs, q_lower), fs.qualified_name)
        )[:MAX_RESULTS]

    logger.debug(
        "workspace_symbol: query=%r, %d flat symbols, %d matches",
        query,
        len(flat),
        len(matches),
    )

    results = []
    for f in matches:
        ws = to_workspace_symbol(f)
        # Add container name from endpoint mirror scope.
        if scope_files is not None and f.file_path:
            import os

            abs_fp = os.path.abspath(f.file_path)
            if abs_fp in scope_files and hasattr(
                indexer, "get_endpoint_mirrors_for_file"
            ):
                mirrors = indexer.get_endpoint_mirrors_for_file(f.file_path)
                if mirrors:
                    container = os.path.basename(mirrors[0]).replace(".ivy", "")
                    ws.container_name = container
        results.append(ws)
    return results


def register(server) -> None:
    """Register the ``workspace/symbol`` feature handler on *server*.

    Args:
        server: An :class:`IvyLanguageServer` instance.
    """

    @server.feature(lsp.WORKSPACE_SYMBOL)
    async def workspace_symbol(
        params: lsp.WorkspaceSymbolParams,
    ) -> List[lsp.WorkspaceSymbol]:
        if server.indexer is None:
            if getattr(server, "initializing", False):
                return [
                    lsp.WorkspaceSymbol(
                        name="[Ivy LSP is still indexing...]",
                        kind=lsp.SymbolKind.Null,
                        location=lsp.Location(
                            uri="",
                            range=lsp.Range(
                                start=lsp.Position(line=0, character=0),
                                end=lsp.Position(line=0, character=0),
                            ),
                        ),
                    )
                ]
            return []

        # Resolve active file path from last didOpen/didChange URI.
        active_filepath = None
        last_uri = getattr(server, "_last_active_uri", None)
        if last_uri:
            from ivy_lsp.utils import uri_to_path

            active_filepath = uri_to_path(last_uri)

        logger.info(
            "workspace/symbol: query=%r, active_uri=%r",
            params.query,
            last_uri,
        )

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            compute_workspace_symbols,
            server.indexer,
            params.query or "",
            active_filepath,
        )

        from ivy_lsp.debug_trace import get_tracer

        tracer = get_tracer()
        if tracer is not None:
            tracer.trace_lsp_request(
                method="workspace/symbol",
                filepath=active_filepath or "(none)",
                word=params.query or None,
                result_summary=f"{len(result)} symbols",
            )

        return result
