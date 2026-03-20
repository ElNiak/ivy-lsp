"""Call hierarchy feature for Ivy LSP.

Provides ``prepareCallHierarchy``, ``incomingCalls``, and ``outgoingCalls``
handlers.

In Ivy:

* **Incoming calls** to action ``foo`` are: other actions whose bodies
  reference ``foo``, plus ``before foo`` / ``after foo`` monitor blocks.
* **Outgoing calls** from action ``foo`` are: action names referenced in
  ``foo``'s body and in its associated ``before``/``after`` blocks.

When a :class:`~ivy_lsp.semantic.model.SemanticModel` is available the
handlers first query ``CALLS`` / ``MONITORS`` edges and only fall back to
the brute-force regex scanning when the model path yields no results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from lsprotocol import types as lsp

from ivy_lsp.utils import uri_to_path
from ivy_lsp.utils.position_utils import make_range, word_at_position

if TYPE_CHECKING:
    from ivy_lsp.semantic.model import SemanticModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_containing_symbol(symbols: List, target_line: int):
    """Find the symbol whose range encloses *target_line*.

    Scans a flat list of ``IvySymbol`` (with ``.range`` = (sl, sc, el, ec))
    and returns the one whose start line is closest to *target_line* without
    exceeding it.  This is a best-effort heuristic since Ivy symbols from
    the fallback scanner only have single-line ranges.
    """
    best = None
    best_line = -1
    for sym in symbols:
        sym_line = sym.range[0]  # 0-based start line
        if sym_line <= target_line and sym_line > best_line:
            best = sym
            best_line = sym_line
    return best


def _is_monitor_symbol(sym) -> bool:
    """Return True if *sym* is a before/after/around monitor."""
    detail = (sym.detail or "").strip().lower()
    return (
        detail.startswith("before ")
        or detail.startswith("after ")
        or detail.startswith("around ")
    )


def _is_action_symbol(sym) -> bool:
    """Return True if *sym* is an action/relation/function declaration (not a monitor).

    Actions use SymbolKind.Method; pure functions/relations use
    SymbolKind.Function.  Both are callable, so both are eligible for
    call hierarchy.
    """
    from lsprotocol.types import SymbolKind

    if sym.kind not in (SymbolKind.Function, SymbolKind.Method):
        return False
    return not _is_monitor_symbol(sym)


def _get_action_symbols(indexer) -> Dict[str, List]:
    """Build a name → [IvySymbol] map for all action-like symbols."""
    result: Dict[str, List] = defaultdict(list)
    for sym in indexer.lookup_all_symbols():
        if _is_action_symbol(sym):
            result[sym.name].append(sym)
    return result


# ---------------------------------------------------------------------------
# Semantic-model helpers
# ---------------------------------------------------------------------------


def _find_symbol_node_id(
    model: SemanticModel, name: str, filepath: str
) -> Optional[str]:
    """Find a :class:`SymbolNode` ID in *model* matching *name* and *file*."""
    from ivy_lsp.semantic.nodes import SymbolNode

    last = name.rsplit(".", 1)[-1] if "." in name else name
    # Prefer match scoped to the file.
    for sn in model.get_nodes_by_type(SymbolNode):
        if (sn.name == last or sn.qualified_name == name) and (
            not filepath or sn.file == filepath
        ):
            return sn.id
    # Fallback: match by name only.
    for sn in model.get_nodes_by_type(SymbolNode):
        if sn.name == last or sn.qualified_name == name:
            return sn.id
    return None


def _node_to_call_hierarchy_item(
    model: SemanticModel, node_id: str
) -> Optional[lsp.CallHierarchyItem]:
    """Convert a :class:`SymbolNode` to a :class:`CallHierarchyItem`."""
    node = model.get_node(node_id)
    if node is None:
        return None

    from lsprotocol.types import SymbolKind

    filepath = getattr(node, "file", "")
    if filepath:
        p = Path(filepath)
        uri = p.as_uri() if p.is_absolute() else ""
    else:
        uri = ""
    line = getattr(node, "line", 0)
    name = getattr(node, "name", node_id.split(":")[-1])

    r = make_range(line, 0, line, len(name))
    return lsp.CallHierarchyItem(
        name=name,
        kind=SymbolKind.Function,
        uri=uri,
        range=r,
        selection_range=r,
        detail=getattr(node, "kind", ""),
    )


# ---------------------------------------------------------------------------
# prepareCallHierarchy
# ---------------------------------------------------------------------------


def prepare_call_hierarchy(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
) -> Optional[List[lsp.CallHierarchyItem]]:
    """Return a CallHierarchyItem for the action/monitor at cursor."""
    word = word_at_position(source_lines, position)
    if not word:
        return None

    results = indexer.lookup_symbol(word)
    if not results and "." in word:
        last = word.rsplit(".", 1)[1]
        results = indexer.lookup_symbol(last)

    if not results:
        return None

    # Rank results — prefer same-layer matches
    if len(results) > 1 and hasattr(indexer, "get_scope_files_for_file"):
        from ivy_lsp.utils.scope_ranking import rank_by_scope

        scope_files = indexer.get_scope_files_for_file(filepath)
        resolver = getattr(indexer, "resolver", None)
        results = rank_by_scope(results, filepath, scope_files, resolver=resolver)
    sl = results[0]
    sym = sl.symbol

    # Only actions and monitors make sense for call hierarchy.
    from lsprotocol.types import SymbolKind

    if sym.kind not in (SymbolKind.Function, SymbolKind.Method):
        return None

    uri = Path(sl.filepath).as_uri() if sl.filepath else ""
    r = make_range(*sl.range)

    item = lsp.CallHierarchyItem(
        name=sym.name,
        kind=sym.kind,
        uri=uri,
        range=r,
        selection_range=r,
        detail=sym.detail or "",
        data=json.dumps({"name": word, "filepath": sl.filepath or ""}),
    )

    return [item]


# ---------------------------------------------------------------------------
# incomingCalls
# ---------------------------------------------------------------------------


def get_incoming_calls(
    indexer,
    item_name: str,
    item_filepath: str,
    model: Optional[SemanticModel] = None,
) -> List[lsp.CallHierarchyIncomingCall]:
    """Find all actions/monitors that reference *item_name*.

    When *model* is provided the semantic graph is queried first; the
    regex-based scanner is used as a fallback.
    """
    # Try semantic model first.
    if model is not None:
        node_id = _find_symbol_node_id(model, item_name, item_filepath)
        if node_id is not None:
            from ivy_lsp.semantic.edges import SemanticEdgeType

            results: List[lsp.CallHierarchyIncomingCall] = []

            # CALLS edges (callers).
            for etype, src_id in model.get_incoming(node_id):
                if etype == SemanticEdgeType.CALLS:
                    item = _node_to_call_hierarchy_item(model, src_id)
                    if item:
                        results.append(
                            lsp.CallHierarchyIncomingCall(
                                from_=item,
                                from_ranges=[item.range],
                            )
                        )

            # MONITORS edges (monitors of this action).
            for etype, src_id in model.get_incoming(node_id):
                if etype == SemanticEdgeType.MONITORS:
                    item = _node_to_call_hierarchy_item(model, src_id)
                    if item:
                        results.append(
                            lsp.CallHierarchyIncomingCall(
                                from_=item,
                                from_ranges=[item.range],
                            )
                        )

            if results:
                return results

    # Fallback to regex scanning.
    return _get_incoming_calls_regex(indexer, item_name, item_filepath)


def _get_incoming_calls_regex(
    indexer,
    item_name: str,
    item_filepath: str,
) -> List[lsp.CallHierarchyIncomingCall]:
    """Regex-based incoming-call scanner (brute-force fallback)."""
    # Search for references across the workspace (same approach as references.py).
    last_component = item_name.rsplit(".", 1)[-1] if "." in item_name else item_name
    pattern = re.compile(r"\b" + re.escape(last_component) + r"\b")

    all_files = indexer.resolver.find_all_ivy_files()

    # Group references by their containing symbol.
    callers: Dict[str, Dict] = {}  # key = "filepath:line" of containing symbol

    for fpath in all_files:
        try:
            source = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        file_lines = source.split("\n")
        file_symbols = indexer.get_symbols(fpath)

        for line_no, line in enumerate(file_lines):
            for match in pattern.finditer(line):
                # Skip the declaration itself.
                abs_fpath = str(Path(fpath).resolve())
                abs_item = str(Path(item_filepath).resolve()) if item_filepath else ""
                if abs_fpath == abs_item:
                    # Check if this is on the symbol's own declaration line.
                    results = indexer.lookup_symbol(item_name)
                    if results and any(sl.range[0] == line_no for sl in results):
                        continue

                # Find the containing symbol for this reference.
                container = _find_containing_symbol(file_symbols, line_no)
                if container is None:
                    continue
                # Skip if the container IS the target symbol itself.
                if container.name == last_component and not _is_monitor_symbol(
                    container
                ):
                    continue

                key = f"{fpath}:{container.range[0]}"
                if key not in callers:
                    callers[key] = {
                        "symbol": container,
                        "filepath": fpath,
                        "from_ranges": [],
                    }
                callers[key]["from_ranges"].append(
                    make_range(line_no, match.start(), line_no, match.end())
                )

    # Convert to CallHierarchyIncomingCall objects.
    results = []
    for info in callers.values():
        sym = info["symbol"]
        fpath = info["filepath"]
        uri = Path(fpath).as_uri()
        r = make_range(*sym.range)

        from_item = lsp.CallHierarchyItem(
            name=sym.name,
            kind=sym.kind,
            uri=uri,
            range=r,
            selection_range=r,
            detail=sym.detail or "",
        )
        results.append(
            lsp.CallHierarchyIncomingCall(
                from_=from_item,
                from_ranges=info["from_ranges"],
            )
        )

    return results


# ---------------------------------------------------------------------------
# outgoingCalls
# ---------------------------------------------------------------------------


def get_outgoing_calls(
    indexer,
    item_name: str,
    item_filepath: str,
    model: Optional[SemanticModel] = None,
) -> List[lsp.CallHierarchyOutgoingCall]:
    """Find all actions referenced in the body of *item_name*.

    When *model* is provided the semantic graph is queried first; the
    regex-based scanner is used as a fallback.
    """
    # Try semantic model first.
    if model is not None:
        node_id = _find_symbol_node_id(model, item_name, item_filepath)
        if node_id is not None:
            from ivy_lsp.semantic.edges import SemanticEdgeType

            results: List[lsp.CallHierarchyOutgoingCall] = []

            for etype, tgt_id in model.get_outgoing(node_id):
                if etype == SemanticEdgeType.CALLS:
                    item = _node_to_call_hierarchy_item(model, tgt_id)
                    if item:
                        results.append(
                            lsp.CallHierarchyOutgoingCall(
                                to=item,
                                from_ranges=[item.range],
                            )
                        )

            if results:
                return results

    # Fallback to regex scanning.
    return _get_outgoing_calls_regex(indexer, item_name, item_filepath)


def _get_outgoing_calls_regex(
    indexer,
    item_name: str,
    item_filepath: str,
) -> List[lsp.CallHierarchyOutgoingCall]:
    """Regex-based outgoing-call scanner (brute-force fallback)."""
    # Get the body text: lines after the declaration up to the next declaration.
    try:
        source = Path(item_filepath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    file_lines = source.split("\n")
    file_symbols = indexer.get_symbols(item_filepath)

    # Find the declaration line for item_name.
    last_component = item_name.rsplit(".", 1)[-1] if "." in item_name else item_name
    decl_line = None
    for sym in file_symbols:
        if sym.name == last_component or sym.name == item_name:
            decl_line = sym.range[0]  # 0-based
            break

    if decl_line is None:
        return []

    # Find the end of this symbol's scope: next top-level declaration line or EOF.
    sorted_lines = sorted(set(s.range[0] for s in file_symbols))
    idx = sorted_lines.index(decl_line) if decl_line in sorted_lines else -1
    if idx >= 0 and idx + 1 < len(sorted_lines):
        end_line = sorted_lines[idx + 1]
    else:
        end_line = len(file_lines)

    # Extract body lines.
    body_lines = file_lines[decl_line + 1 : end_line]

    # Build a set of known action names in the workspace.
    action_symbols = _get_action_symbols(indexer)

    # Scan body for references to known actions.
    outgoing: Dict[str, Dict] = {}

    for action_name, action_locs in action_symbols.items():
        if action_name == last_component:
            continue  # Skip self-references for outgoing.

        pattern = re.compile(r"\b" + re.escape(action_name) + r"\b")
        for body_line_idx, body_line in enumerate(body_lines):
            for match in pattern.finditer(body_line):
                actual_line = decl_line + 1 + body_line_idx
                if action_name not in outgoing:
                    # Use the first location of the action as the target.
                    target_sym = action_locs[0]
                    target_uri = (
                        Path(target_sym.file_path).as_uri()
                        if target_sym.file_path
                        else ""
                    )
                    target_range = make_range(*target_sym.range)
                    outgoing[action_name] = {
                        "to": lsp.CallHierarchyItem(
                            name=target_sym.name,
                            kind=target_sym.kind,
                            uri=target_uri,
                            range=target_range,
                            selection_range=target_range,
                            detail=target_sym.detail or "",
                        ),
                        "from_ranges": [],
                    }
                outgoing[action_name]["from_ranges"].append(
                    make_range(actual_line, match.start(), actual_line, match.end())
                )

    return [
        lsp.CallHierarchyOutgoingCall(
            to=info["to"],
            from_ranges=info["from_ranges"],
        )
        for info in outgoing.values()
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(server) -> None:
    """Register call hierarchy feature handlers."""

    @server.feature(lsp.TEXT_DOCUMENT_PREPARE_CALL_HIERARCHY)
    async def prepare(
        params: lsp.CallHierarchyPrepareParams,
    ) -> Optional[List[lsp.CallHierarchyItem]]:
        """Handle textDocument/prepareCallHierarchy requests."""
        try:
            uri = params.text_document.uri
            doc = server.workspace.get_text_document(uri)
            if server.indexer is None:
                return None
            lines = doc.source.split("\n") if doc.source else []
            filepath = uri_to_path(uri)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                prepare_call_hierarchy,
                server.indexer,
                filepath,
                params.position,
                lines,
            )
        except Exception:
            logger.warning("prepareCallHierarchy handler failed", exc_info=True)
            return None

    @server.feature(lsp.CALL_HIERARCHY_INCOMING_CALLS)
    async def incoming_calls(
        params: lsp.CallHierarchyIncomingCallsParams,
    ) -> Optional[List[lsp.CallHierarchyIncomingCall]]:
        """Handle callHierarchy/incomingCalls requests."""
        try:
            if server.indexer is None:
                return None
            item = params.item
            data = (
                json.loads(item.data)
                if isinstance(item.data, str)
                else (item.data or {})
            )
            name = data.get("name", item.name)
            filepath = data.get("filepath", uri_to_path(item.uri))
            sem_model = getattr(server, "semantic_model", None)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                get_incoming_calls,
                server.indexer,
                name,
                filepath,
                sem_model,
            )
        except Exception:
            logger.warning("incomingCalls handler failed", exc_info=True)
            return None

    @server.feature(lsp.CALL_HIERARCHY_OUTGOING_CALLS)
    async def outgoing_calls(
        params: lsp.CallHierarchyOutgoingCallsParams,
    ) -> Optional[List[lsp.CallHierarchyOutgoingCall]]:
        """Handle callHierarchy/outgoingCalls requests."""
        try:
            if server.indexer is None:
                return None
            item = params.item
            data = (
                json.loads(item.data)
                if isinstance(item.data, str)
                else (item.data or {})
            )
            name = data.get("name", item.name)
            filepath = data.get("filepath", uri_to_path(item.uri))
            sem_model = getattr(server, "semantic_model", None)
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                get_outgoing_calls,
                server.indexer,
                name,
                filepath,
                sem_model,
            )
        except Exception:
            logger.warning("outgoingCalls handler failed", exc_info=True)
            return None
