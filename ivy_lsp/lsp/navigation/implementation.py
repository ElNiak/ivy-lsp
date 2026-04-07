"""Go-to-implementation feature for Ivy LSP.

In Ivy, ``before`` and ``after`` monitors are the closest analogue to
"implementations" of an action.  This handler:

* Cursor on an **action declaration** → returns locations of all
  ``before``/``after`` blocks for that action across the workspace.
* Cursor on a **before/after block** → returns the action declaration
  (reverse direction).
* Fallback: delegates to ``goto_definition``.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Union

from lsprotocol import types as lsp

from ivy_lsp.infra.utils.name_utils import get_last_component
from ivy_lsp.infra.utils.position_utils import word_at_position
from ivy_lsp.infra.utils.symbol_resolver import lookup_with_dotted_fallback
from ivy_lsp.lsp.navigation._handler import (
    scoped_lookup_to_location,
    symbol_to_location,
)

logger = logging.getLogger(__name__)

# Regex matching before/after declarations (start of line).
_MONITOR_RE = re.compile(r"^\s*(before|after|around)\s+")


def goto_implementation(
    indexer,
    filepath: str,
    position: lsp.Position,
    source_lines: List[str],
) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
    """Find implementations of the symbol at cursor.

    For actions: find ``before``/``after`` monitor blocks.
    For monitors: find the action declaration (reverse lookup).
    """
    word = word_at_position(source_lines, position)
    if not word:
        return None

    # Determine if cursor is on a before/after line (reverse lookup).
    on_monitor = False
    if position.line < len(source_lines):
        line_text = source_lines[position.line]
        if _MONITOR_RE.match(line_text):
            on_monitor = True

    if on_monitor:
        # Reverse: cursor is on ``before foo`` → find action ``foo`` declaration.
        return _find_action_declaration(indexer, word, filepath=filepath)
    else:
        # Forward: cursor is on ``action foo`` → find before/after blocks.
        monitors = _find_monitors_for_action(indexer, word)
        if monitors:
            return monitors if len(monitors) != 1 else monitors[0]
        # Fallback: delegate to goto_definition.
        from ivy_lsp.lsp.navigation.definition import goto_definition as _goto_def

        return _goto_def(indexer, filepath, position, source_lines)


def _find_monitors_for_action(
    indexer,
    action_name: str,
) -> List[lsp.Location]:
    """Scan all workspace symbols for before/after blocks matching *action_name*."""
    locations: List[lsp.Location] = []
    last_component = get_last_component(action_name)

    for sym in indexer.lookup_all_symbols():
        detail = (sym.detail or "").strip()
        detail_lower = detail.lower()

        # The fallback scanner produces symbols like:
        #   name="connect", detail="before connect(src:cid, dst:cid)", kind=Function
        if not (
            detail_lower.startswith("before ")
            or detail_lower.startswith("after ")
            or detail_lower.startswith("around ")
        ):
            continue

        # Check if this monitor targets the action we're looking for.
        # The symbol name is the dotted path after before/after (e.g. "connect" or "foo.step").
        if sym.name == action_name or sym.name == last_component:
            locations.append(symbol_to_location(sym))

    return locations


def _find_action_declaration(
    indexer,
    action_name: str,
    filepath: Optional[str] = None,
) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
    """Find the declaration of an action by name (excluding monitors)."""
    results = lookup_with_dotted_fallback(indexer, action_name)

    # Filter out before/after symbols — we want the declaration only.
    filtered = []
    for sl in results:
        detail = (sl.symbol.detail or "").strip().lower()
        if (
            detail.startswith("before ")
            or detail.startswith("after ")
            or detail.startswith("around ")
        ):
            continue
        filtered.append(sl)

    if not filtered:
        return None

    # Rank by scope + layer awareness when multiple results
    if filepath and len(filtered) > 1 and hasattr(indexer, "get_scope_files_for_file"):
        from ivy_lsp.infra.utils.scope_ranking import rank_by_scope

        scope_files = indexer.get_scope_files_for_file(filepath)
        resolver = getattr(indexer, "resolver", None)
        filtered = rank_by_scope(filtered, filepath, scope_files, resolver=resolver)

    locations = [scoped_lookup_to_location(sl) for sl in filtered]

    if len(locations) == 1:
        return locations[0]
    return locations


def register(server) -> None:
    """Register the ``textDocument/implementation`` feature handler."""
    from ivy_lsp.lsp.navigation._handler import run_navigation_handler

    @server.feature(lsp.TEXT_DOCUMENT_IMPLEMENTATION)
    async def implementation(
        params: lsp.ImplementationParams,
    ) -> Optional[Union[lsp.Location, List[lsp.Location]]]:
        """Handle textDocument/implementation requests."""
        return await run_navigation_handler(
            params,
            server,
            lambda ctx: goto_implementation(
                ctx.indexer,
                ctx.filepath,
                ctx.position,
                ctx.lines,
            ),
            track_active_uri=True,
            trace_method="textDocument/implementation",
        )
