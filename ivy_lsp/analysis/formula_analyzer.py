"""Extract state variable references from Ivy formula AST nodes and text.

Provides both AST-based extraction (full mode, walking Atom/App nodes) and
text-based tokenization (light mode fallback).
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Set

logger = logging.getLogger(__name__)

# Tokenizer for formula text: identifiers and dotted paths.
_IDENT_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.]*")


def extract_state_var_references(
    formula_node: Any, known_vars: Set[str]
) -> List[str]:
    """Walk a formula AST node and return referenced state variable names.

    Traverses ``Atom`` and ``App`` nodes recursively, matching ``.relname``
    against *known_vars*.  Returns a deduplicated list preserving first-seen
    order.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _walk(node: Any) -> None:
        if node is None:
            return

        relname = getattr(node, "relname", None)
        if relname and relname in known_vars and relname not in seen:
            seen.add(relname)
            found.append(relname)

        rep = getattr(node, "rep", None)
        if rep and rep in known_vars and rep not in seen:
            seen.add(rep)
            found.append(rep)

        for arg in getattr(node, "args", ()):
            _walk(arg)

    _walk(formula_node)
    return found


def extract_state_var_references_text(
    formula_text: str, known_vars: Set[str]
) -> List[str]:
    """Tokenize formula text and match against known state variable names.

    This is the fallback for light mode or when the AST node is unavailable.
    Handles dotted names (e.g. ``frame.ack.sent_pkt``) by matching both the
    full dotted path and each suffix segment.
    """
    if not formula_text or not known_vars:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for m in _IDENT_RE.finditer(formula_text):
        token = m.group()
        # Try full match first
        if token in known_vars and token not in seen:
            seen.add(token)
            found.append(token)
            continue

        # Try suffix segments: "a.b.c" -> check "b.c", "c"
        parts = token.split(".")
        for i in range(1, len(parts)):
            suffix = ".".join(parts[i:])
            if suffix in known_vars and suffix not in seen:
                seen.add(suffix)
                found.append(suffix)

        # Also check each individual part
        for part in parts:
            if part in known_vars and part not in seen:
                seen.add(part)
                found.append(part)

    return found
