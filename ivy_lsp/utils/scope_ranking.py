"""Scope- and layer-aware ranking for symbol lookup results."""

import os
from typing import Optional


def rank_by_scope(
    results: list,
    current_filepath: str,
    scope_files: set,
    resolver=None,
) -> list:
    """Rank symbol results by scope relevance + layer awareness.

    Priority: same-file(0) > in-scope+same-dir(1) > in-scope(2) > same-dir(3)
              > same-layer(4) > different-layer/unknown(5)
    """
    current_norm = os.path.normpath(os.path.abspath(current_filepath))
    current_dir = os.path.dirname(current_norm)

    # Determine requesting file's layer (if layer staging active)
    current_layer: Optional[str] = None
    if resolver and hasattr(resolver, "_file_to_layer"):
        current_layer = resolver._file_to_layer.get(current_norm)

    def _score(r):
        # Support both SymbolLocation.filepath and IvySymbol.file_path
        raw = getattr(r, "filepath", None) or getattr(r, "file_path", None) or ""
        rpath = os.path.normpath(os.path.abspath(raw))
        in_scope = rpath in scope_files
        if rpath == current_norm:
            return (0, 0)
        if in_scope and os.path.dirname(rpath) == current_dir:
            return (1, 0)
        if in_scope:
            return (2, 0)
        if os.path.dirname(rpath) == current_dir:
            return (3, 0)
        # Layer-aware: same layer ranks higher than different layer
        if current_layer and resolver and hasattr(resolver, "_file_to_layer"):
            r_layer = resolver._file_to_layer.get(rpath)
            if r_layer == current_layer:
                return (4, 0)  # same layer, out of scope
            if r_layer is not None:
                return (5, 0)  # different layer
        return (5, 0)

    return sorted(results, key=_score)
