"""Scope- and layer-aware ranking for symbol lookup results."""

import os
from typing import Optional, Set


def get_layer_scope(resolver, filepath: str) -> Optional[Set[str]]:
    """Return the set of layer IDs visible from *filepath*.

    Includes the file's own layer plus all upstream ``depends_on`` layers
    (transitively).  Returns ``None`` when layer staging is not active,
    meaning no filtering should be applied.
    """
    if (
        not resolver
        or not hasattr(resolver, "_file_to_layer")
        or not resolver._file_to_layer
    ):
        return None

    norm = os.path.normpath(os.path.abspath(filepath))
    current_layer = resolver._file_to_layer.get(norm)
    if current_layer is None:
        return None

    layer_by_id = getattr(resolver, "_layer_by_id", {})
    if not layer_by_id:
        return None

    # Walk depends_on upward (transitively) to collect visible layers.
    visible: Set[str] = set()
    queue = [current_layer]
    while queue:
        lid = queue.pop()
        if lid in visible:
            continue
        visible.add(lid)
        layer_obj = layer_by_id.get(lid)
        if layer_obj and hasattr(layer_obj, "depends_on"):
            for dep in layer_obj.depends_on:
                if dep not in visible:
                    queue.append(dep)

    return visible


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

    # Layer-aware soft partition: prefer layer-visible results, fall back
    # to cross-layer only when no visible results exist.
    visible_layers = get_layer_scope(resolver, current_filepath)

    if visible_layers is None:
        return sorted(results, key=_score)

    layer_visible = []
    layer_external = []
    for r in results:
        raw = getattr(r, "filepath", None) or getattr(r, "file_path", None) or ""
        rpath = os.path.normpath(os.path.abspath(raw))
        r_layer = resolver._file_to_layer.get(rpath)
        # Unmapped files (stdlib, etc.) and files in visible layers stay visible
        if r_layer is None or r_layer in visible_layers:
            layer_visible.append(r)
        else:
            layer_external.append(r)

    if layer_visible:
        return sorted(layer_visible, key=_score)
    return sorted(layer_external, key=_score)
