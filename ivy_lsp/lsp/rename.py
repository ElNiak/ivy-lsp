"""textDocument/rename feature handler.

Provides workspace-wide rename of Ivy symbols using regex-based
text matching, following the same pattern as find_references().
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Dict, List, Optional

from lsprotocol import types as lsp

from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.infra.utils.name_utils import get_last_component
from ivy_lsp.infra.utils.position_utils import make_range, word_at_position


def compute_rename(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    new_name: str,
) -> Optional[lsp.WorkspaceEdit]:
    """Compute a workspace edit that renames the symbol under the cursor.

    Uses text-based regex matching across all workspace files,
    mirroring the approach in find_references().

    Args:
        indexer: The :class:`WorkspaceIndexer` instance providing access to
            the workspace file list via ``indexer.resolver.find_all_ivy_files()``.
        filepath: Absolute path of the document containing the cursor.
        position: The cursor position (0-based line and character).
        source_lines: The source of the current document split into lines.
        new_name: The new name to replace the symbol with.

    Returns:
        A :class:`lsp.WorkspaceEdit` mapping file URIs to lists of
        :class:`lsp.TextEdit`, or ``None`` when the word under the cursor
        is empty, the indexer is unavailable, or no matches are found.
    """
    word = word_at_position(source_lines, position)
    if not word:
        return None

    name = get_last_component(word)
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")

    if indexer is None:
        return None

    all_files = indexer.resolver.find_all_ivy_files()
    changes: Dict[str, List[lsp.TextEdit]] = {}

    for fpath in all_files:
        try:
            file_source = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_lines = file_source.split("\n")
        edits: List[lsp.TextEdit] = []

        for line_no, line in enumerate(file_lines):
            for match in pattern.finditer(line):
                edits.append(
                    lsp.TextEdit(
                        range=make_range(line_no, match.start(), line_no, match.end()),
                        new_text=new_name,
                    )
                )

        if edits:
            uri = Path(fpath).as_uri()
            changes[uri] = edits

    if not changes:
        return None

    return lsp.WorkspaceEdit(changes=changes)


def register(server) -> None:
    """Register the ``textDocument/rename`` feature handler.

    Args:
        server: The pygls ``LanguageServer`` instance to register on.
    """

    @server.feature(lsp.TEXT_DOCUMENT_RENAME)
    async def rename(params: lsp.RenameParams) -> Optional[lsp.WorkspaceEdit]:
        """Handle textDocument/rename requests."""
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not doc.source:
            return None
        lines = doc.source.split("\n")
        filepath = uri_to_path(uri)
        indexer = server.indexer
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            compute_rename,
            indexer,
            filepath,
            params.position,
            lines,
            params.new_name,
        )
