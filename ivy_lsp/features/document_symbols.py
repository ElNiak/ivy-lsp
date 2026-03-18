"""textDocument/documentSymbol feature handler.

Converts ``IvySymbol`` trees to LSP ``DocumentSymbol`` trees and registers
the ``textDocument/documentSymbol`` request handler on the server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from lsprotocol import types as lsp

from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import make_range

logger = logging.getLogger(__name__)

_ZERO_RANGE = (0, 0, 0, 0)


def _filter_zero_range(symbols: List[IvySymbol]) -> List[IvySymbol]:
    """Remove symbols with (0,0,0,0) range — included-file leaks.

    When the Ivy parser merges ``include``d modules into the AST, their
    declarations often lack location data.  ``_loc_to_tuple()`` falls back
    to ``(0, 0, 0, 0)`` for these, which renders as "Line 1" in the LSP
    client.  Filtering them out keeps documentSymbol results scoped to
    symbols actually defined in the requested file.

    Applies recursively to children so nested leaks are also removed.
    """
    result: List[IvySymbol] = []
    for s in symbols:
        if s.range == _ZERO_RANGE:
            continue
        if s.children:
            s.children = _filter_zero_range(s.children)
        result.append(s)
    return result


def ivy_symbol_to_document_symbol(sym: IvySymbol) -> lsp.DocumentSymbol:
    """Convert an IvySymbol to an LSP DocumentSymbol.

    Children are converted recursively.  An empty ``children`` list on the
    input symbol produces ``children=None`` on the output (the LSP spec
    treats ``None`` and ``[]`` differently in some clients).

    Args:
        sym: The Ivy symbol to convert.

    Returns:
        An LSP DocumentSymbol with matching name, kind, range, detail,
        and recursively-converted children.
    """
    r = make_range(*sym.range)
    children: Optional[List[lsp.DocumentSymbol]] = None
    if sym.children:
        children = [ivy_symbol_to_document_symbol(c) for c in sym.children]
    return lsp.DocumentSymbol(
        name=sym.name,
        kind=sym.kind,
        range=r,
        selection_range=r,
        detail=sym.detail,
        children=children,
    )


def ivy_symbols_to_document_symbols(
    symbols: List[IvySymbol],
) -> List[lsp.DocumentSymbol]:
    """Convert a list of IvySymbols to LSP DocumentSymbols.

    Args:
        symbols: The Ivy symbols to convert.

    Returns:
        A list of LSP DocumentSymbols in the same order.
    """
    return [ivy_symbol_to_document_symbol(s) for s in symbols]


def get_document_symbols(
    symbols: Optional[List[IvySymbol]],
) -> List[lsp.DocumentSymbol]:
    """Null-safe wrapper for document symbol conversion.

    Args:
        symbols: An optional list of Ivy symbols.  ``None`` and ``[]``
            both produce an empty result.

    Returns:
        A (possibly empty) list of LSP DocumentSymbols.
    """
    if not symbols:
        return []
    return ivy_symbols_to_document_symbols(symbols)


def compute_document_symbols(
    parser,
    indexer,
    source: str,
    filepath: str,
) -> List[lsp.DocumentSymbol]:
    """Parse source and return LSP DocumentSymbol list.

    Strategy:
    1. If parser available: parse source (short 0.5s timeout), convert AST
    2. If parse fails: fallback scanner
    3. If lock busy (TimeoutError): use cached indexed symbols, then fallback
    4. If no parser but indexer available: use cached indexed symbols
    5. If neither: return empty list
    """
    # Demand-driven deep parse for shared modules
    if indexer is not None and hasattr(indexer, "deep_parse_on_demand"):
        indexer.deep_parse_on_demand(filepath)

    symbols: List[IvySymbol] = []

    if parser is not None and source:
        from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
        from ivy_lsp.parsing.fallback_scanner import fallback_scan

        try:
            result = parser.parse(source, filepath, timeout=0.5)
            if result.success and result.ast is not None:
                symbols = _filter_zero_range(
                    ast_to_symbols(result.ast, filepath, source)
                )
            else:
                symbols, _error_info = fallback_scan(source, filepath)
        except TimeoutError:
            logger.debug("Parser lock busy, using cached symbols for %s", filepath)
            if indexer is not None:
                symbols = indexer.get_symbols(filepath) or []
            if not symbols:
                symbols, _error_info = fallback_scan(source, filepath)
    elif indexer is not None:
        symbols = indexer.get_symbols(filepath) or []

    return get_document_symbols(symbols)


def register(server) -> None:
    """Register the ``textDocument/documentSymbol`` feature handler.

    Args:
        server: The pygls ``LanguageServer`` instance to register on.
    """

    @server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
    async def document_symbol(
        params: lsp.DocumentSymbolParams,
    ) -> List[lsp.DocumentSymbol]:
        """Handle textDocument/documentSymbol requests."""
        try:
            uri = params.text_document.uri
            server._last_active_uri = uri
            doc = server.workspace.get_text_document(uri)
            filepath = uri_to_path(uri)
            source = doc.source or ""
            parser = getattr(server, "parser", None)
            indexer = getattr(server, "indexer", None)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                compute_document_symbols,
                parser,
                indexer,
                source,
                filepath,
            )
        except Exception:
            logger.warning("document_symbol handler failed", exc_info=True)
            return []
