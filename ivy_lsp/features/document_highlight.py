"""textDocument/documentHighlight feature handler.

Highlights all occurrences of the symbol under the cursor within
the current document.
"""

from __future__ import annotations

import re
from typing import List, Optional

from lsprotocol import types as lsp

from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import make_range, word_at_position


def compute_document_highlights(
    source_lines: List[str],
    position: lsp.Position,
    symbols: Optional[list] = None,
) -> List[lsp.DocumentHighlight]:
    """Find all occurrences of the word under the cursor in the document.

    If *symbols* are provided, the occurrence at a symbol's declaration
    range is marked as ``Write``; all others are ``Read``.
    """
    word = word_at_position(source_lines, position)
    if not word:
        return []

    name = word.rsplit(".", 1)[-1] if "." in word else word
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")

    definition_lines: set[int] = set()
    if symbols:
        for sym in symbols:
            if sym.name == name:
                definition_lines.add(sym.range[0])

    highlights: List[lsp.DocumentHighlight] = []
    for line_no, line in enumerate(source_lines):
        for match in pattern.finditer(line):
            kind = (
                lsp.DocumentHighlightKind.Write
                if line_no in definition_lines
                else lsp.DocumentHighlightKind.Read
            )
            highlights.append(
                lsp.DocumentHighlight(
                    range=make_range(line_no, match.start(), line_no, match.end()),
                    kind=kind,
                )
            )

    return highlights


def register(server) -> None:
    """Register the ``textDocument/documentHighlight`` feature handler."""

    @server.feature(lsp.TEXT_DOCUMENT_DOCUMENT_HIGHLIGHT)
    def document_highlight(
        params: lsp.DocumentHighlightParams,
    ) -> Optional[List[lsp.DocumentHighlight]]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not doc.source:
            return None
        lines = doc.source.split("\n")
        filepath = uri_to_path(uri)
        indexer = getattr(server, "_indexer", None)
        symbols = indexer.get_symbols(filepath) if indexer else None
        return compute_document_highlights(lines, params.position, symbols=symbols)
