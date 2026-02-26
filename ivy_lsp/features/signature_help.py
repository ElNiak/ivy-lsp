"""textDocument/signatureHelp feature handler.

Provides parameter hints when typing function/action/relation calls.
Triggers on ``(`` and ``,`` characters.
"""

from __future__ import annotations

import re
from typing import List, Optional

from lsprotocol import types as lsp


def _parse_parameters(detail: Optional[str]) -> List[lsp.ParameterInformation]:
    """Extract parameter information from an IvySymbol detail string.

    Handles formats like:
    - ``"(src:cid, dst:cid)"``
    - ``"action send(src:cid, dst:cid)"``
    """
    if not detail:
        return []

    # Extract the parenthesized portion
    paren_match = re.search(r"\(([^)]*)\)", detail)
    if not paren_match:
        return []

    inner = paren_match.group(1).strip()
    if not inner:
        return []

    params: List[lsp.ParameterInformation] = []
    for part in inner.split(","):
        part = part.strip()
        if part:
            params.append(lsp.ParameterInformation(label=part))

    return params


def _find_call_context(
    line_text: str, character: int
) -> Optional[tuple[str, int]]:
    """Find the function name and active parameter index at cursor position.

    Scans backwards from cursor to find the enclosing ``(`` and counts
    commas to determine the active parameter index.

    Returns (function_name, active_param_index) or None.
    """
    text_before = line_text[:character]

    # Walk backward to find the matching open paren
    depth = 0
    comma_count = 0
    for i in range(len(text_before) - 1, -1, -1):
        ch = text_before[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth > 0:
                depth -= 1
            else:
                # Found the matching open paren
                # Extract function name before it
                prefix = text_before[:i].rstrip()
                name_match = re.search(r"(\w+)$", prefix)
                if name_match:
                    return (name_match.group(1), comma_count)
                return None
        elif ch == "," and depth == 0:
            comma_count += 1

    return None


def compute_signature_help(
    indexer,
    filepath: str,
    source_lines: List[str],
    position: lsp.Position,
) -> Optional[lsp.SignatureHelp]:
    """Compute signature help for a call expression at the cursor.

    Looks backward from the cursor position to find the function name
    and active parameter, then looks up the symbol's detail string
    to extract parameter information.
    """
    if position.line < 0 or position.line >= len(source_lines):
        return None

    line = source_lines[position.line]
    ctx = _find_call_context(line, position.character)
    if ctx is None:
        return None

    func_name, active_param = ctx

    if indexer is None:
        return None

    # Look up the symbol to get its detail (signature) string
    locations = indexer.lookup_symbol(func_name)
    if not locations:
        return None

    sym = locations[0].symbol
    params = _parse_parameters(sym.detail)

    # Build the signature label from the original detail string
    paren_match = re.search(r"\(.*\)", sym.detail) if sym.detail else None
    label = f"{func_name}{paren_match.group(0)}" if paren_match else func_name

    clamped = min(active_param, max(0, len(params) - 1)) if params else 0

    sig = lsp.SignatureInformation(
        label=label,
        parameters=params if params else None,
        active_parameter=clamped,
    )

    return lsp.SignatureHelp(
        signatures=[sig],
        active_signature=0,
        active_parameter=clamped,
    )


def register(server) -> None:
    """Register the ``textDocument/signatureHelp`` feature handler."""

    @server.feature(
        lsp.TEXT_DOCUMENT_SIGNATURE_HELP,
        lsp.SignatureHelpOptions(
            trigger_characters=["(", ","],
        ),
    )
    def signature_help(
        params: lsp.SignatureHelpParams,
    ) -> Optional[lsp.SignatureHelp]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not doc.source:
            return None
        lines = doc.source.split("\n")
        filepath = uri.replace("file://", "")
        indexer = getattr(server, "_indexer", None)
        return compute_signature_help(indexer, filepath, lines, params.position)
