"""textDocument/selectionRange feature handler.

Builds nested selection chains: word -> line -> brace block -> file.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

from lsprotocol import types as lsp

from ivy_lsp.utils.position_utils import make_range, word_at_position


def _find_enclosing_brace_block(
    lines: List[str], line_no: int
) -> Optional[tuple[int, int]]:
    """Find the innermost brace block enclosing the given line.

    Returns (start_line, end_line) or None.
    """
    blocks: List[tuple[int, int]] = []
    stack: List[int] = []
    for i, line in enumerate(lines):
        for ch in line:
            if ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                open_line = stack.pop()
                if open_line != i:
                    blocks.append((open_line, i))

    best: Optional[tuple[int, int]] = None
    for start, end in blocks:
        if start <= line_no <= end:
            if best is None or (end - start) < (best[1] - best[0]):
                best = (start, end)
    return best


def compute_selection_ranges(
    source_lines: List[str],
    positions: Sequence[lsp.Position],
) -> List[lsp.SelectionRange]:
    """Compute selection range chains for each requested position.

    Chain: word -> line -> brace block (if any) -> whole file.
    """
    total_lines = len(source_lines)
    last_line_len = len(source_lines[-1]) if source_lines else 0
    file_range = make_range(0, 0, max(0, total_lines - 1), last_line_len)

    results: List[lsp.SelectionRange] = []
    for pos in positions:
        chain: List[lsp.Range] = []

        # 1. Word under cursor
        word = word_at_position(source_lines, pos)
        if word:
            line = source_lines[pos.line]
            for m in re.finditer(r"\b" + re.escape(word) + r"\b", line):
                if m.start() <= pos.character <= m.end():
                    chain.append(make_range(pos.line, m.start(), pos.line, m.end()))
                    break

        # 2. Full line
        if pos.line < total_lines:
            line_len = len(source_lines[pos.line])
            chain.append(make_range(pos.line, 0, pos.line, line_len))

        # 3. Enclosing brace block
        block = _find_enclosing_brace_block(source_lines, pos.line)
        if block:
            block_end_len = (
                len(source_lines[block[1]]) if block[1] < total_lines else 0
            )
            chain.append(make_range(block[0], 0, block[1], block_end_len))

        # 4. Whole file
        chain.append(file_range)

        # Deduplicate consecutive identical ranges.
        deduped: List[lsp.Range] = []
        for r in chain:
            if not deduped or (
                r.start != deduped[-1].start or r.end != deduped[-1].end
            ):
                deduped.append(r)

        # Build linked list from innermost to outermost.
        sr: Optional[lsp.SelectionRange] = None
        for r in reversed(deduped):
            sr = lsp.SelectionRange(range=r, parent=sr)
        if sr is None:
            sr = lsp.SelectionRange(range=file_range, parent=None)
        results.append(sr)

    return results


def register(server) -> None:
    """Register the ``textDocument/selectionRange`` feature handler."""

    @server.feature(lsp.TEXT_DOCUMENT_SELECTION_RANGE)
    def selection_range(
        params: lsp.SelectionRangeParams,
    ) -> Optional[List[lsp.SelectionRange]]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not doc.source:
            return None
        lines = doc.source.split("\n")
        return compute_selection_ranges(lines, params.positions)
