"""Shared symbol resolution helpers used by multiple LSP feature handlers.

Extracts common patterns for symbol lookup with dotted fallback and
demand-driven deep parsing that were duplicated across definition.py,
hover.py, call_hierarchy.py, implementation.py, and document_symbols.py.
"""

from __future__ import annotations

from typing import Any, List, Optional


def lookup_with_dotted_fallback(indexer: Any, word: str) -> List[Any]:
    """Look up a symbol with progressive dotted suffix fallback.

    Tries the full word first, then progressively strips leading
    dot-separated components until a match is found.

    Example: "quic.frame.type" tries "quic.frame.type", then
    "frame.type", then "type".
    """
    results = indexer.lookup_symbol(word)
    if not results and "." in word:
        parts = word.split(".")
        for i in range(1, len(parts)):
            suffix = ".".join(parts[i:])
            results = indexer.lookup_symbol(suffix)
            if results:
                break
    return results


def ensure_deep_parsed(indexer: Optional[Any], filepath: str) -> None:
    """Trigger demand-driven deep parse if the indexer supports it.

    No-op if indexer is None or lacks deep_parse_on_demand.
    """
    if indexer is not None and hasattr(indexer, "deep_parse_on_demand"):
        indexer.deep_parse_on_demand(filepath)
