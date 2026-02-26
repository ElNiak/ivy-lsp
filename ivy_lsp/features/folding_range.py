"""textDocument/foldingRange feature handler.

Provides code folding for brace-delimited blocks, consecutive comment
lines, and consecutive include directives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from lsprotocol import types as lsp


@dataclass
class FoldRange:
    start_line: int
    end_line: int
    kind: str  # "region", "comment", "imports"


def compute_folding_ranges(source: str) -> List[FoldRange]:
    """Compute folding ranges from Ivy source text.

    Detects:
    - Brace blocks ``{ ... }`` spanning multiple lines
    - Consecutive ``#`` comment lines (3+ lines)
    - Consecutive ``include`` directives (2+ lines)
    """
    if not source:
        return []

    lines = source.split("\n")
    ranges: List[FoldRange] = []

    # --- Brace blocks ---
    brace_stack: List[int] = []
    for i, line in enumerate(lines):
        for ch in line:
            if ch == "{":
                brace_stack.append(i)
            elif ch == "}" and brace_stack:
                open_line = brace_stack.pop()
                if i > open_line:
                    ranges.append(FoldRange(open_line, i, "region"))

    # --- Consecutive comment lines ---
    comment_start: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_comment = stripped.startswith("#") and not stripped.startswith("#lang")
        if is_comment:
            if comment_start is None:
                comment_start = i
        else:
            if comment_start is not None and i - comment_start >= 3:
                ranges.append(FoldRange(comment_start, i - 1, "comment"))
            comment_start = None
    if comment_start is not None and len(lines) - comment_start >= 3:
        ranges.append(FoldRange(comment_start, len(lines) - 1, "comment"))

    # --- Consecutive include directives ---
    include_start: Optional[int] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("include "):
            if include_start is None:
                include_start = i
        else:
            if include_start is not None and i - include_start >= 2:
                ranges.append(FoldRange(include_start, i - 1, "imports"))
            include_start = None
    if include_start is not None and len(lines) - include_start >= 2:
        ranges.append(FoldRange(include_start, len(lines) - 1, "imports"))

    return ranges


def _to_lsp_folding_range(fr: FoldRange) -> lsp.FoldingRange:
    kind_map = {
        "comment": lsp.FoldingRangeKind.Comment,
        "imports": lsp.FoldingRangeKind.Imports,
        "region": lsp.FoldingRangeKind.Region,
    }
    return lsp.FoldingRange(
        start_line=fr.start_line,
        end_line=fr.end_line,
        kind=kind_map.get(fr.kind),
    )


def register(server) -> None:
    """Register the ``textDocument/foldingRange`` feature handler."""

    @server.feature(lsp.TEXT_DOCUMENT_FOLDING_RANGE)
    def folding_range(
        params: lsp.FoldingRangeParams,
    ) -> List[lsp.FoldingRange]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        source = doc.source or ""
        fold_ranges = compute_folding_ranges(source)
        return [_to_lsp_folding_range(fr) for fr in fold_ranges]
