"""Shared regex-based reference extraction for call/instance/monitor patterns.

Consolidates the reference-extraction loops that were duplicated between
``TieredExtractor._try_lexer()`` (Tier 2) and ``._try_regex()`` (Tier 3).
Both tiers use the same three regex patterns to find call statements,
instance declarations, and monitor (before/after/around) blocks, then
build ``SymbolReference`` objects.  The only input they differ on is the
*symbols* list used to locate enclosing actions for call references --
Tier 2 passes lexer-produced symbols while Tier 3 passes regex-produced
symbols.

This module exposes a single public helper, ``extract_references_regex``,
that both tiers now call.
"""

from __future__ import annotations

import re
from typing import List, Optional

from lsprotocol.types import SymbolKind

from ivy_lsp.core.parsing.symbols import IvySymbol, SymbolReference

# ---------------------------------------------------------------------------
# Regex patterns for reference extraction
# ---------------------------------------------------------------------------

_CALL_STMT_RE = re.compile(r"(?:call\s+)([\w.]+)\s*(?:\(|;|$)", re.MULTILINE)
_INSTANCE_RE = re.compile(r"^\s*instance\s+([\w.]+)\s*:\s*([\w.]+)", re.MULTILINE)
_MONITOR_RE = re.compile(r"^\s*(before|after|around)\s+([\w.]+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_enclosing_action(symbols: List[IvySymbol], line_idx: int) -> Optional[str]:
    """Return the name of the action whose range best contains *line_idx*.

    Scans *symbols* for ``Function`` / ``Method`` kinds whose start line
    is at or before *line_idx*, picking the closest (largest start line).
    Returns ``None`` when no enclosing action is found.
    """
    best_name: Optional[str] = None
    best_line = -1
    for sym in symbols:
        if (
            sym.kind in (SymbolKind.Function, SymbolKind.Method)
            and sym.range[0] <= line_idx
            and sym.range[0] > best_line
        ):
            best_name = sym.name
            best_line = sym.range[0]
    return best_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_references_regex(
    source: str,
    filepath: str,
    declaration_symbols: List[IvySymbol],
) -> List[SymbolReference]:
    """Extract call/instance/monitor references from *source* using regex.

    Args:
        source: The full Ivy source text.
        filepath: Path to the source file (attached to each ``SymbolReference``).
        declaration_symbols: Previously-extracted symbols used to find the
            enclosing action for ``call`` references.  This is the list
            produced by either Tier 2 (lexer) or Tier 3 (regex) before
            reference extraction runs.

    Returns:
        All discovered call, instance, and monitor references.
    """
    references: List[SymbolReference] = []

    # -- CALLS: call X(...) ------------------------------------------------
    for m in _CALL_STMT_RE.finditer(source):
        target = m.group(1)
        call_line = source[: m.start()].count("\n")
        enclosing = _find_enclosing_action(declaration_symbols, call_line)
        if enclosing:
            references.append(
                SymbolReference(
                    source_name=enclosing,
                    target_name=target,
                    kind="call",
                    line=call_line,
                    file_path=filepath,
                )
            )

    # -- INSTANCES: instance X : Y(...) ------------------------------------
    for m in _INSTANCE_RE.finditer(source):
        references.append(
            SymbolReference(
                source_name=m.group(1),
                target_name=m.group(2),
                kind="instance",
                line=source[: m.start()].count("\n"),
                file_path=filepath,
            )
        )

    # -- MONITORS: before/after/around X -----------------------------------
    for m in _MONITOR_RE.finditer(source):
        mixin_kind = m.group(1)  # "before", "after", "around"
        action_name = m.group(2)
        references.append(
            SymbolReference(
                source_name=f"{mixin_kind} {action_name}",
                target_name=action_name,
                kind="monitor",
                line=source[: m.start()].count("\n"),
                file_path=filepath,
            )
        )

    return references
