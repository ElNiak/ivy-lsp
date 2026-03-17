"""Find references feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import List, Optional

from lsprotocol import types as lsp

from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import make_range, word_at_position


def find_references(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    include_declaration: bool = True,
) -> List[lsp.Location]:
    """Find all references to the symbol at the given position.

    Extracts the word under the cursor, then scans every ``.ivy`` file in the
    workspace for whole-word matches, returning an LSP ``Location`` for each.

    Args:
        indexer: The :class:`WorkspaceIndexer` instance providing access to
            the workspace file list via ``indexer.resolver.find_all_ivy_files()``.
        filepath: Absolute path of the document containing the cursor.
        position: The cursor position (0-based line and character).
        source_lines: The source of the current document split into lines.
        include_declaration: Whether to include the declaration site itself
            among the returned locations.  When ``False``, the match at the
            cursor position in the current file is excluded.

    Returns:
        A list of :class:`lsp.Location` objects, one per match found.
        Returns an empty list when the word under the cursor is empty
        or no matches are found.
    """
    word = word_at_position(source_lines, position)
    if not word:
        return []

    # For dotted names like ``frame.ack``, match only the last component.
    name = word.rsplit(".", 1)[-1] if "." in word else word
    pattern = re.compile(r"\b" + re.escape(name) + r"\b")

    all_files = indexer.resolver.find_all_ivy_files()

    abs_filepath = str(Path(filepath).resolve())
    # H4: Ensure the queried file is always in the scan list
    resolved_set = {str(Path(f).resolve()) for f in all_files}
    if abs_filepath not in resolved_set:
        all_files = list(all_files) + [abs_filepath]
    cursor_line = position.line

    locations: List[lsp.Location] = []
    for fpath in all_files:
        try:
            file_source = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        abs_fpath = str(Path(fpath).resolve())
        file_lines = file_source.split("\n")
        for line_no, line in enumerate(file_lines):
            for match in pattern.finditer(line):
                # Filter out the declaration (cursor position) when requested
                if not include_declaration:
                    if (
                        abs_fpath == abs_filepath
                        and line_no == cursor_line
                        and match.start() <= position.character < match.end()
                    ):
                        continue
                uri = Path(fpath).as_uri()
                r = make_range(line_no, match.start(), line_no, match.end())
                locations.append(lsp.Location(uri=uri, range=r))

    return locations


def register(server) -> None:
    """Register the ``textDocument/references`` feature handler.

    Args:
        server: The pygls ``LanguageServer`` instance to register on.
    """

    @server.feature(lsp.TEXT_DOCUMENT_REFERENCES)
    async def references(
        params: lsp.ReferenceParams,
    ) -> Optional[List[lsp.Location]]:
        """Handle textDocument/references requests."""
        uri = params.text_document.uri
        server._last_active_uri = uri
        doc = server.workspace.get_text_document(uri)
        if server.indexer is None:
            return None
        lines = doc.source.split("\n") if doc.source else []
        filepath = uri_to_path(uri)
        include_decl = params.context.include_declaration if params.context else True
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            find_references,
            server.indexer,
            filepath,
            params.position,
            lines,
            include_decl,
        )
