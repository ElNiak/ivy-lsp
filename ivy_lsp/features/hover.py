"""Hover information feature for Ivy LSP."""

from __future__ import annotations

import os
from typing import List, Optional

from lsprotocol import types as lsp
from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import word_at_position

# Map SymbolKind back to Ivy keyword for display.
_KIND_TO_KEYWORD = {
    SymbolKind.Class: "type",
    SymbolKind.Function: "action",
    SymbolKind.Module: "object",
    SymbolKind.Variable: "individual",
    SymbolKind.Property: "property",
    SymbolKind.Namespace: "isolate",
    SymbolKind.File: "include",
    SymbolKind.Field: "destructor",
    SymbolKind.EnumMember: "constructor",
}

# Detail prefixes that override the kind-based keyword.
_DETAIL_KEYWORDS = frozenset(
    {
        "action",
        "relation",
        "function",
        "object",
        "module",
        "alias",
        "property",
        "axiom",
        "conjecture",
        "invariant",
        "destructor",
        "constructor",
        "instance",
    }
)


def format_hover_content(symbol: Optional[IvySymbol]) -> Optional[str]:
    """Format an IvySymbol as a Markdown hover string."""
    if symbol is None:
        return None

    keyword = _KIND_TO_KEYWORD.get(symbol.kind, "")
    detail = symbol.detail or ""

    # Check if detail itself is a keyword override (fallback scanner pattern)
    if detail and detail.split()[0] in _DETAIL_KEYWORDS:
        keyword = detail.split()[0]
        detail = " ".join(detail.split()[1:])

    # Build the signature line
    if detail.startswith("(") or detail.startswith("returns"):
        sig = f"{keyword} {symbol.name}{detail}"
    elif detail.startswith("enum:"):
        variants = detail[len("enum:") :].strip()
        sig = f"{keyword} {symbol.name} = {{{variants}}}"
    elif detail:
        sig = f"{keyword} {symbol.name} {detail}".rstrip()
    else:
        sig = f"{keyword} {symbol.name}"

    lines = [f"```ivy\n{sig}\n```"]

    if symbol.file_path:
        basename = os.path.basename(symbol.file_path)
        lines.append(f"\n*Defined in: {basename}*")

    return "\n".join(lines)


def _enrich_with_semantic_model(
    content: str,
    symbol_name: str,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    semantic_model,
) -> str:
    """Add SemanticModel info (type details, RFC annotations, xrefs) to hover."""
    if semantic_model is None:
        return content

    from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode

    extra_parts: List[str] = []

    # Check if cursor line has RFC annotation
    line = position.line
    abs_path = os.path.abspath(filepath)
    annotations = [
        n
        for n in semantic_model.get_nodes_by_type(RfcAnnotation)
        if n.file
        and (n.file == abs_path or n.file == filepath or os.path.abspath(n.file) == abs_path)
        and n.line == line
    ]
    if annotations:
        for ann in annotations:
            for tag in ann.tags:
                req = semantic_model.get_node(tag)
                if req and isinstance(req, RfcRequirement):
                    extra_parts.append(
                        f"\n**{req.rfc} {req.section}** ({req.level}): {req.text[:120]}"
                    )

    # Enrich with sort/arity from SymbolNode
    symbol_nodes = semantic_model.get_nodes_by_type(SymbolNode)
    for sn in symbol_nodes:
        if sn.name == symbol_name or sn.qualified_name == symbol_name:
            if sn.params:
                param_str = ", ".join(sn.params)
                extra_parts.append(f"\n*Params:* `({param_str})`")
            if sn.return_sort:
                extra_parts.append(f"*Returns:* `{sn.return_sort}`")
            if sn.sort_name and sn.sort_name != "action":
                extra_parts.append(f"*Sort:* `{sn.sort_name}`")
            break

    # Cross-reference summary
    for sn in symbol_nodes:
        if sn.name == symbol_name:
            incoming = semantic_model.get_incoming(sn.id)
            outgoing = semantic_model.get_outgoing(sn.id)
            if incoming or outgoing:
                extra_parts.append(
                    f"\n*References:* {len(incoming)} incoming, {len(outgoing)} outgoing"
                )
            break

    if extra_parts:
        content += "\n\n---\n" + "\n".join(extra_parts)

    return content


def get_hover_info(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    semantic_model=None,
) -> Optional[lsp.Hover]:
    """Look up symbol at cursor and return formatted Hover."""
    word = word_at_position(source_lines, position)
    if not word:
        return None

    results = indexer.lookup_symbol(word)
    if not results and "." in word:
        last = word.rsplit(".", 1)[1]
        results = indexer.lookup_symbol(last)

    if not results:
        return None

    sym = results[0].symbol
    content = format_hover_content(sym)
    if content is None:
        return None

    content = _enrich_with_semantic_model(
        content, word, filepath, position, source_lines, semantic_model
    )

    return lsp.Hover(
        contents=lsp.MarkupContent(
            kind=lsp.MarkupKind.Markdown,
            value=content,
        ),
    )


def register(server) -> None:
    """Register the textDocument/hover feature handler."""

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    def hover(params: lsp.HoverParams) -> Optional[lsp.Hover]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not hasattr(server, "_indexer") or server._indexer is None:
            return None
        lines = doc.source.split("\n") if doc.source else []
        filepath = uri_to_path(uri)
        model = getattr(server, "_semantic_model", None)
        return get_hover_info(
            server._indexer, filepath, params.position, lines, model
        )
