"""Hover information feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional

from lsprotocol import types as lsp
from lsprotocol.types import SymbolKind

from ivy_lsp.core.parsing.symbols import IvySymbol
from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.infra.utils.position_utils import word_at_position
from ivy_lsp.infra.utils.symbol_resolver import (
    ensure_deep_parsed,
    lookup_with_dotted_fallback,
)

logger = logging.getLogger(__name__)

# Map SymbolKind back to Ivy keyword for display.
_KIND_TO_KEYWORD = {
    SymbolKind.Class: "type",
    SymbolKind.Function: "function",
    SymbolKind.Method: "action",
    SymbolKind.Module: "object",
    SymbolKind.Variable: "individual",
    SymbolKind.Property: "property",
    SymbolKind.Namespace: "isolate",
    SymbolKind.File: "include",
    SymbolKind.Field: "destructor",
    SymbolKind.EnumMember: "constructor",
    SymbolKind.TypeParameter: "interpret",
    SymbolKind.Interface: "schema",
    SymbolKind.Event: "export",
    SymbolKind.String: "native",
    SymbolKind.Constant: "attribute",
}

# Detail prefixes that override the kind-based keyword.
_DETAIL_KEYWORDS = frozenset(
    {
        "type",
        "action",
        "relation",
        "function",
        "object",
        "module",
        "isolate",
        "alias",
        "property",
        "axiom",
        "conjecture",
        "invariant",
        "destructor",
        "constructor",
        "instance",
        "individual",
        "interpret",
    }
)


def _relative_path(filepath: str) -> str:
    """Shorten an absolute path to a relative one from protocol-testing/ or ivy/include/."""
    for marker in ("protocol-testing/", "ivy/include/"):
        idx = filepath.find(marker)
        if idx != -1:
            return filepath[idx:]
    return os.path.basename(filepath)


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
        rel_path = _relative_path(symbol.file_path)
        line_num = symbol.range[0] + 1 if symbol.range[0] > 0 else None
        if line_num:
            lines.append(f"\n*Defined in: {rel_path}:{line_num}*")
        else:
            lines.append(f"\n*Defined in: {rel_path}*")

    # Show children count for container symbols (objects, modules)
    if symbol.children:
        n = len(symbol.children)
        lines.append(f"*Members:* {n}")

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

    from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement, SymbolNode

    extra_parts: List[str] = []

    # Check if cursor line has RFC annotation
    line = position.line
    abs_path = os.path.abspath(filepath)
    annotations = [
        n
        for n in semantic_model.get_nodes_by_type(RfcAnnotation)
        if n.file
        and (
            n.file == abs_path
            or n.file == filepath
            or os.path.abspath(n.file) == abs_path
        )
        and n.line == line
    ]
    if annotations:
        for ann in annotations:
            for tag in ann.tags:
                req = semantic_model.get_node(tag)
                if req and isinstance(req, RfcRequirement):
                    extra_parts.append(
                        f"\n**{req.rfc} {req.section}** ({req.level}): {req.text}"
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


def _hover_from_semantic_model(
    word: str,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    semantic_model,
) -> Optional[lsp.Hover]:
    """Build hover info from the SemanticModel when the indexer has no cache hit.

    Queries SymbolNode entries by name and synthesizes a hover response
    with type details, RFC annotations, and cross-reference counts.
    """
    if semantic_model is None:
        return None

    from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

    # Search SymbolNode entries matching the word
    symbol_nodes = semantic_model.get_nodes_by_type(SymbolNode)
    matches = [
        sn for sn in symbol_nodes if sn.name == word or sn.qualified_name == word
    ]
    if not matches and "." in word:
        last = word.rsplit(".", 1)[-1]
        by_last = [sn for sn in symbol_nodes if sn.name == last]
        suffix = [sn for sn in by_last if sn.qualified_name.endswith(word)]
        matches = suffix if suffix else by_last

    # Also check TypeNode
    type_matches = [
        tn
        for tn in semantic_model.get_nodes_by_type(TypeNode)
        if tn.name == word or tn.qualified_name == word
    ]

    if not matches and not type_matches:
        return None

    # Build hover content from the semantic model data
    parts: List[str] = []

    if matches:
        sn = matches[0]
        keyword = sn.kind or "symbol"
        param_str = ""
        if sn.params:
            param_str = "(" + ", ".join(sn.params) + ")"
        ret_str = ""
        if sn.return_sort:
            ret_str = f" : {sn.return_sort}"
        sig = f"{keyword} {sn.qualified_name}{param_str}{ret_str}"
        parts.append(f"```ivy\n{sig}\n```")
        if sn.file:
            rel = _relative_path(sn.file)
            parts.append(f"\n*Defined in: {rel}:{sn.line}*")
    elif type_matches:
        tn = type_matches[0]
        if tn.is_enum and tn.variants:
            sig = f"type {tn.qualified_name} = {{{', '.join(tn.variants)}}}"
        else:
            sig = f"type {tn.qualified_name}"
        parts.append(f"```ivy\n{sig}\n```")
        if tn.file:
            rel = _relative_path(tn.file)
            parts.append(f"\n*Defined in: {rel}:{tn.line}*")

    if not parts:
        return None

    content = "\n".join(parts)

    # Enrich with full semantic model details (xrefs, RFC annotations, etc.)
    content = _enrich_with_semantic_model(
        content, word, filepath, position, source_lines, semantic_model
    )

    return lsp.Hover(
        contents=lsp.MarkupContent(
            kind=lsp.MarkupKind.Markdown,
            value=content,
        ),
    )


def _enrich_with_mirror_context(
    content: str,
    symbol_name: str,
    filepath: str,
    indexer,
) -> str:
    """Add endpoint mirror test context to hover content.

    Shows which mirror test scopes contain the symbol's defining file,
    helping the user understand cross-scope visibility.
    """
    if not hasattr(indexer, "get_endpoint_mirrors_for_file"):
        return content

    # Find which mirrors contain the current file
    mirrors = indexer.get_endpoint_mirrors_for_file(filepath)
    if not mirrors:
        return content

    mirror_names = [os.path.basename(m).replace(".ivy", "") for m in mirrors[:20]]
    if len(mirrors) > 20:
        mirror_names.append(f"... +{len(mirrors) - 20} more")

    content += f"\n\n*Endpoint mirrors:* {', '.join(mirror_names)}"
    return content


def get_hover_info(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    semantic_model=None,
) -> Optional[lsp.Hover]:
    """Look up symbol at cursor and return formatted Hover."""
    # Demand-driven deep parse for shared modules
    ensure_deep_parsed(indexer, filepath)

    word = word_at_position(source_lines, position)
    if not word:
        return None

    results = lookup_with_dotted_fallback(indexer, word)

    if not results:
        # Fallback: query the SemanticModel directly when the indexer
        # hasn't cached this symbol (e.g., symbols from included files
        # that were indexed but not yet cached by the indexer).
        return _hover_from_semantic_model(
            word, filepath, position, source_lines, semantic_model
        )

    from ivy_lsp.infra.utils.scope_ranking import rank_by_scope

    scope_files = set()
    if hasattr(indexer, "get_scope_files_for_file"):
        scope_files = indexer.get_scope_files_for_file(filepath)
    resolver = getattr(indexer, "resolver", None)
    results = rank_by_scope(results, filepath, scope_files, resolver=resolver)
    sym = results[0].symbol
    content = format_hover_content(sym)
    if content is None:
        return None

    content = _enrich_with_semantic_model(
        content, word, filepath, position, source_lines, semantic_model
    )

    # Add endpoint mirror scope context
    content = _enrich_with_mirror_context(content, word, filepath, indexer)

    return lsp.Hover(
        contents=lsp.MarkupContent(
            kind=lsp.MarkupKind.Markdown,
            value=content,
        ),
    )


def register(server) -> None:
    """Register the textDocument/hover feature handler."""

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    async def hover(params: lsp.HoverParams) -> Optional[lsp.Hover]:
        try:
            uri = params.text_document.uri
            server._last_active_uri = uri
            doc = server.workspace.get_text_document(uri)
            if server.indexer is None:
                return None
            lines = doc.source.split("\n") if doc.source else []
            filepath = uri_to_path(uri)
            model = server.semantic_model
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                get_hover_info,
                server.indexer,
                filepath,
                params.position,
                lines,
                model,
            )

            from ivy_lsp.infra.observability import get_tracer

            tracer = get_tracer()
            if tracer is not None:
                word = word_at_position(lines, params.position) if lines else None
                content_len = 0
                if result and result.contents:
                    content_len = len(getattr(result.contents, "value", ""))
                tracer.trace_lsp_request(
                    method="textDocument/hover",
                    filepath=filepath,
                    position=f"{params.position.line}:{params.position.character}",
                    word=word,
                    result_summary=(
                        f"Hover content, {content_len} chars" if result else None
                    ),
                )

            return result
        except Exception:
            logger.warning("hover handler failed", exc_info=True)
            return None
