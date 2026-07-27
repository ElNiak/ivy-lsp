"""Index artifact persistence: write manifest, symbols, includes, exports, requirements."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def write_index_artifacts(
    index_dir: str,
    manifest: dict,
    symbols_map: Dict[str, list],
    includes_map: Dict[str, list],
    includes_raw: Dict[str, List[str]],
    exports_map: Dict[str, dict],
    requirements_map: Dict[str, list],
    scopes: Dict | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
) -> None:
    """Write all index artifacts to *index_dir*.

    Creates the directory if it doesn't exist. Writes JSON artifacts
    and optional pickle files for semantic model and requirement graph.
    """
    os.makedirs(index_dir, exist_ok=True)

    _write_json(os.path.join(index_dir, "manifest.json"), manifest)
    _write_json(os.path.join(index_dir, "symbols.json"), symbols_map)
    _write_json(os.path.join(index_dir, "includes.json"), includes_map)
    _write_json(os.path.join(index_dir, "includes_raw.json"), includes_raw)
    _write_json(os.path.join(index_dir, "exports.json"), exports_map)
    _write_json(os.path.join(index_dir, "requirements.json"), requirements_map)

    if scopes is not None:
        _write_scopes(index_dir, scopes)

    if semantic_model is not None:
        _write_pickle(index_dir, "semantic_model.pickle.gz", semantic_model)

    if requirement_graph is not None:
        _write_pickle(index_dir, "requirement_graph.pickle.gz", requirement_graph)


def write_health_report(index_dir: str, health: dict) -> None:
    """Write health report JSON to *index_dir*."""
    _write_json(os.path.join(index_dir, "health.json"), health)


def _write_json(path: str, data: Any) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except OSError:
        logger.warning("Failed to write %s", path, exc_info=True)


def _write_scopes(index_dir: str, scopes: Dict) -> None:
    # _meta.json is a list of scope dicts, matching index_builder.py ordering.
    scopes_dir = os.path.join(index_dir, "scopes")
    os.makedirs(scopes_dir, exist_ok=True)
    meta_entries: list = []
    for test_name, scope in sorted(scopes.items()):
        scope_dict = scope.to_dict() if hasattr(scope, "to_dict") else scope
        _write_json(os.path.join(scopes_dir, f"{test_name}.json"), scope_dict)
        meta_entries.append(scope_dict)
    _write_json(os.path.join(scopes_dir, "_meta.json"), meta_entries)


def _write_pickle(index_dir: str, filename: str, obj: Any) -> None:
    from ivy_lsp.infra.utils.serialization import write_locked_pickle

    write_locked_pickle(index_dir, filename, obj, logger)
