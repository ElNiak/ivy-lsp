"""Completion feature for Ivy LSP."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from lsprotocol import types as lsp
from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol
from ivy_lsp.utils import uri_to_path

logger = logging.getLogger(__name__)

MAX_COMPLETIONS = 300

# Load keywords from ivy lexer with frozen fallback.
try:
    from ivy.ivy_lexer import all_reserved

    IVY_KEYWORDS: List[str] = sorted(all_reserved.keys())
except ImportError:
    IVY_KEYWORDS = [
        "action",
        "after",
        "alias",
        "around",
        "assert",
        "assume",
        "attribute",
        "axiom",
        "before",
        "call",
        "class",
        "common",
        "concept",
        "conjecture",
        "constructor",
        "debug",
        "decreases",
        "definition",
        "delegate",
        "derived",
        "destructor",
        "else",
        "ensure",
        "ensures",
        "entry",
        "eventually",
        "execute",
        "exists",
        "explicit",
        "export",
        "extract",
        "false",
        "field",
        "finite",
        "for",
        "forall",
        "forget",
        "fresh",
        "from",
        "function",
        "ghost",
        "global",
        "globally",
        "if",
        "implement",
        "implementation",
        "import",
        "in",
        "include",
        "individual",
        "init",
        "instance",
        "instantiate",
        "interpret",
        "invariant",
        "isa",
        "isolate",
        "let",
        "local",
        "macro",
        "match",
        "maximizing",
        "method",
        "minimizing",
        "mixin",
        "mixord",
        "module",
        "named",
        "null",
        "object",
        "of",
        "old",
        "parameter",
        "params",
        "private",
        "process",
        "progress",
        "proof",
        "property",
        "relation",
        "rely",
        "require",
        "requires",
        "returns",
        "scenario",
        "schema",
        "set",
        "some",
        "specification",
        "state",
        "struct",
        "subclass",
        "tactic",
        "temporal",
        "template",
        "theorem",
        "this",
        "thunk",
        "trigger",
        "true",
        "trusted",
        "type",
        "unfold",
        "unprovable",
        "update",
        "using",
        "var",
        "variant",
        "while",
        "with",
    ]

IVY_KEYWORDS_SET: Set[str] = set(IVY_KEYWORDS)


class CompletionContext(enum.Enum):
    DOT_ACCESS = "dot_access"
    INCLUDE = "include"
    AFTER_KEYWORD = "after_keyword"
    GENERAL = "general"


_KIND_TO_COMPLETION = {
    SymbolKind.Class: lsp.CompletionItemKind.Class,
    SymbolKind.Function: lsp.CompletionItemKind.Function,
    SymbolKind.Module: lsp.CompletionItemKind.Module,
    SymbolKind.Variable: lsp.CompletionItemKind.Variable,
    SymbolKind.Property: lsp.CompletionItemKind.Property,
    SymbolKind.Namespace: lsp.CompletionItemKind.Module,
    SymbolKind.Field: lsp.CompletionItemKind.Field,
    SymbolKind.EnumMember: lsp.CompletionItemKind.EnumMember,
}


def _symbol_kind_to_completion_kind(kind: SymbolKind) -> lsp.CompletionItemKind:
    return _KIND_TO_COMPLETION.get(kind, lsp.CompletionItemKind.Text)


def detect_context(
    line_text: str,
    character: int,
) -> Tuple[CompletionContext, str, str]:
    """Detect completion context from text before cursor.

    Returns (context_type, prefix, scope_name).
    """
    text_before = line_text[:character]

    # 1. Dot access: "identifier." or "identifier.partial"
    dot_match = re.search(r"(\w+(?:\.\w+)*)\.(\w*)$", text_before)
    if dot_match:
        return CompletionContext.DOT_ACCESS, dot_match.group(2), dot_match.group(1)

    # 2. Include
    include_match = re.match(r"^\s*include\s+(\w*)$", text_before)
    if include_match:
        return CompletionContext.INCLUDE, include_match.group(1), ""

    # 3. After keyword (C7)
    kw_match = re.match(
        r"^\s*(type|relation|function|individual|instance|action|var"
        r"|before|after|around|module|object|isolate)\s+(\w*)$",
        text_before,
    )
    if kw_match:
        return CompletionContext.AFTER_KEYWORD, kw_match.group(2), kw_match.group(1)

    # 4. General (extract prefix)
    prefix_match = re.search(r"(\w*)$", text_before)
    prefix = prefix_match.group(1) if prefix_match else ""
    return CompletionContext.GENERAL, prefix, ""


def get_completions(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
    requirement_graph: Any = None,
) -> List[lsp.CompletionItem]:
    """Compute completion items for the given position."""
    if position.line < 0 or position.line >= len(source_lines):
        return []

    line_text = source_lines[position.line]
    ctx, prefix, scope_name = detect_context(line_text, position.character)

    if ctx == CompletionContext.DOT_ACCESS:
        return _dot_access_completions(indexer, filepath, scope_name, prefix)
    elif ctx == CompletionContext.INCLUDE:
        return _include_completions(indexer, filepath, prefix)
    elif ctx == CompletionContext.AFTER_KEYWORD:
        return _after_keyword_completions(
            indexer, filepath, prefix, scope_name, requirement_graph
        )
    else:
        items = _general_completions(indexer, filepath, prefix)
        # Merge semantic completions when available (C6)
        if requirement_graph is not None:
            block_type = _detect_block_type(source_lines, position.line)
            sem_dicts = compute_semantic_completions(
                requirement_graph, filepath, position.line, block_type,
            )
            kind_map = {
                "variable": lsp.CompletionItemKind.Variable,
                "function": lsp.CompletionItemKind.Function,
                "class": lsp.CompletionItemKind.Class,
            }
            for d in sem_dicts:
                items.append(
                    lsp.CompletionItem(
                        label=d["label"],
                        kind=kind_map.get(d.get("kind", ""), lsp.CompletionItemKind.Text),
                        detail=d.get("detail"),
                        insert_text=d.get("insertText"),
                        sort_text=d.get("sortText"),
                    )
                )
        return items


def _dot_access_completions(
    indexer, filepath: str, scope_name: str, prefix: str
) -> List[lsp.CompletionItem]:
    """Complete children of a named scope (after '.')."""
    symbols = indexer._symbol_table.lookup_qualified(scope_name)
    if not symbols:
        symbols = indexer._symbol_table.lookup(scope_name)
    if not symbols:
        return []

    items: List[lsp.CompletionItem] = []
    seen: Set[str] = set()
    for parent in symbols:
        for child in parent.children:
            if prefix and not child.name.lower().startswith(prefix.lower()):
                continue
            if child.name in seen:
                continue
            seen.add(child.name)
            items.append(
                lsp.CompletionItem(
                    label=child.name,
                    kind=_symbol_kind_to_completion_kind(child.kind),
                    detail=child.detail,
                )
            )
    return items[:MAX_COMPLETIONS]


def _include_completions(
    indexer, filepath: str, prefix: str
) -> List[lsp.CompletionItem]:
    """Complete include filenames."""
    abs_filepath = os.path.abspath(filepath)
    all_files = indexer._resolver.find_all_ivy_files()
    items: List[lsp.CompletionItem] = []
    seen: Set[str] = set()
    for fpath in all_files:
        if os.path.abspath(fpath) == abs_filepath:
            continue
        name = os.path.splitext(os.path.basename(fpath))[0]
        if name in seen:
            continue
        if prefix and not name.lower().startswith(prefix.lower()):
            continue
        seen.add(name)
        items.append(
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.File,
            )
        )
    return items[:MAX_COMPLETIONS]


def _general_completions(
    indexer, filepath: str, prefix: str
) -> List[lsp.CompletionItem]:
    """Return all symbols in scope + keywords, filtered by prefix."""
    items: List[lsp.CompletionItem] = []
    seen: Set[str] = set()
    lower_prefix = prefix.lower()

    # Symbols in scope
    scope_symbols = indexer.get_symbols_in_scope(filepath)
    for sym in scope_symbols:
        _add_symbol_completions(sym, lower_prefix, seen, items)

    # Keywords
    for kw in IVY_KEYWORDS:
        if kw in seen:
            continue
        if lower_prefix and not kw.startswith(lower_prefix):
            continue
        seen.add(kw)
        items.append(
            lsp.CompletionItem(
                label=kw,
                kind=lsp.CompletionItemKind.Keyword,
            )
        )

    return items[:MAX_COMPLETIONS]


def _add_symbol_completions(
    sym: IvySymbol,
    lower_prefix: str,
    seen: Set[str],
    items: List[lsp.CompletionItem],
) -> None:
    """Add a symbol and its children to the completion list."""
    if sym.name not in seen:
        if not lower_prefix or sym.name.lower().startswith(lower_prefix):
            seen.add(sym.name)
            items.append(
                lsp.CompletionItem(
                    label=sym.name,
                    kind=_symbol_kind_to_completion_kind(sym.kind),
                    detail=sym.detail,
                )
            )
    for child in sym.children:
        _add_symbol_completions(child, lower_prefix, seen, items)


def _detect_block_type(source_lines: List[str], line: int) -> str:
    """Scan backwards from *line* to find if cursor is in a before/after/around block."""
    for i in range(line, -1, -1):
        stripped = source_lines[i].strip()
        if re.match(r"^before\s+", stripped):
            return "before"
        elif re.match(r"^after\s+", stripped):
            return "after"
        elif re.match(r"^around\s+", stripped):
            return "around"
        elif re.match(r"^(action|type|relation|function|module|object|isolate)\s+", stripped):
            return "body"
    return "body"


def _after_keyword_completions(
    indexer,
    filepath: str,
    prefix: str,
    keyword: str,
    requirement_graph: Any = None,
) -> List[lsp.CompletionItem]:
    """Provide completions after a keyword like 'before', 'after', 'type', etc."""
    items: List[lsp.CompletionItem] = []

    if keyword in ("before", "after", "around"):
        # Suggest action names from the graph
        if requirement_graph is not None:
            for action_id in requirement_graph.actions:
                action = requirement_graph.actions[action_id]
                if not prefix or action.name.lower().startswith(prefix.lower()):
                    items.append(
                        lsp.CompletionItem(
                            label=action.name,
                            kind=lsp.CompletionItemKind.Function,
                            detail=f"action ({keyword} monitor)",
                        )
                    )
    elif keyword in ("type", "relation", "function", "individual", "var"):
        # Suggest sort names from the indexer's symbols
        all_symbols = indexer.get_symbols_in_scope(filepath)
        seen: set = set()
        for sym in all_symbols:
            if sym.kind.name in ("Class",) and sym.name not in seen:
                if not prefix or sym.name.lower().startswith(prefix.lower()):
                    items.append(
                        lsp.CompletionItem(
                            label=sym.name,
                            kind=lsp.CompletionItemKind.Class,
                            detail="sort",
                        )
                    )
                    seen.add(sym.name)

    return items


def compute_semantic_completions(
    graph: Any,
    filepath: str,
    line: int,
    block_type: str,
) -> List[Dict[str, str]]:
    """Compute context-aware completion items from the requirement graph.

    Args:
        graph: RequirementGraph instance (or None)
        filepath: Current file path
        line: Current line number
        block_type: "before", "after", or "body"

    Returns:
        List of dicts with label, detail, kind, insertText, sortText keys.
    """
    if graph is None:
        return []

    from ivy_lsp.analysis.requirement_graph import EdgeType

    completions: List[Dict[str, str]] = []

    # Find which action's monitor block we're in
    action_name = _find_enclosing_action(graph, filepath, line)

    if action_name and block_type in ("before", "body"):
        # In before/body blocks: suggest state vars commonly used in require
        reqs = graph.get_requirements_for_action(action_name)
        seen_vars: Set[str] = set()
        for req in reqs:
            for etype, target_id in graph._outgoing.get(req.id, []):
                if etype == EdgeType.READS:
                    seen_vars.add(target_id)

        # Suggest all state vars, prioritizing those already used
        for var_id, var_node in graph.state_vars.items():
            priority = "high" if var_id in seen_vars else "low"
            completions.append({
                "label": var_node.name,
                "detail": f"state var ({priority} relevance)",
                "kind": "variable",
                "insertText": var_node.name,
                "sortText": "0" + var_node.name
                if priority == "high"
                else "1" + var_node.name,
            })

    elif action_name and block_type == "after":
        # In after blocks: suggest state vars commonly written
        written = graph.get_all_state_vars_written()
        for sv in written:
            completions.append({
                "label": sv.name,
                "detail": "state var (written by action)",
                "kind": "variable",
                "insertText": sv.name,
                "sortText": "0" + sv.name,
            })

    return completions


def _find_enclosing_action(
    graph: Any, filepath: str, line: int
) -> Optional[str]:
    """Find the action name for the monitor block at the given line.

    Searches requirements in the graph for one whose file matches and
    whose line is within a proximity window (10 lines) of the cursor.
    """
    for req in graph.requirements.values():
        if req.file == filepath and abs(req.line - line) <= 10:
            return req.monitor_action
    return None


def register(server) -> None:
    """Register the textDocument/completion feature handler."""

    @server.feature(
        lsp.TEXT_DOCUMENT_COMPLETION,
        lsp.CompletionOptions(trigger_characters=[".", " "]),
    )
    async def completion(
        params: lsp.CompletionParams,
    ) -> Optional[List[lsp.CompletionItem]]:
        uri = params.text_document.uri
        doc = server.workspace.get_text_document(uri)
        if not hasattr(server, "_indexer") or server._indexer is None:
            return None
        lines = doc.source.split("\n") if doc.source else []
        filepath = uri_to_path(uri)
        graph = getattr(server._indexer, "_requirement_graph", None)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: get_completions(
                server._indexer, filepath, params.position, lines,
                requirement_graph=graph,
            ),
        )
