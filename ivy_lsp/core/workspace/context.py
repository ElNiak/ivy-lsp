"""Unified workspace context for offline index consumption.

Loads pre-built ``.ivy-index/`` directories (one per protocol) into a
single ``WorkspaceContext`` that downstream consumers (LSP server, MCP
tools, CLI) can query without repeating detection or parsing logic.

The loading sequence is:

1. Detect the Ivy workspace via :func:`workspace_detection.detect_ivy_workspace`.
2. Glob ``protocol-testing/*/.ivy-index/manifest.json`` under the workspace root.
3. For each protocol, load JSON artifacts and optional pickle files into a
   :class:`ProtocolIndex`.
4. Compute staleness by comparing manifest mtimes against actual file mtimes.

Error recovery is strict: corrupt JSON skips that artifact (with a warning),
corrupt pickles yield ``None``, a missing manifest skips the entire protocol.
The loader never fails hard -- it always degrades to an empty index.
"""

from __future__ import annotations

import glob
import gzip
import json
import logging
import os
import pickle
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from ivy_lsp.core.analysis.test_scope import ExportImportInfo, TestScope
from ivy_lsp.core.parsing.symbols import IncludeGraph
from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace
from ivy_lsp.core.workspace.detection import WorkspaceConfig, detect_ivy_workspace
from ivy_lsp.core.workspace.session_overlay import SessionOverlay, TestScopeView

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StalenessInfo:
    """Staleness assessment for a single protocol index."""

    status: Literal["fresh", "stale_minor", "stale_major"]
    changed_files: int
    total_files: int


@dataclass
class ProtocolIndex:
    """All indexed artifacts for a single protocol."""

    protocol: str
    index_dir: str
    manifest: dict
    symbols: Dict[str, List[dict]] = field(default_factory=dict)
    includes: IncludeGraph = field(default_factory=IncludeGraph)
    exports: Dict[str, ExportImportInfo] = field(default_factory=dict)
    scopes: Dict[str, TestScope] = field(default_factory=dict)
    semantic_model: Optional[Any] = None
    requirement_graph: Optional[Any] = None
    staleness: StalenessInfo = field(
        default_factory=lambda: StalenessInfo(
            status="stale_major", changed_files=0, total_files=0
        )
    )


# ---------------------------------------------------------------------------
# WorkspaceContext
# ---------------------------------------------------------------------------


class WorkspaceContext:
    """Unified view of an Ivy workspace with pre-built index data.

    Use the :meth:`load` classmethod as the canonical entry point::

        ctx = WorkspaceContext.load("/path/to/project")
        if ctx.has_index():
            for proto in ctx.list_protocols():
                print(proto, ctx.protocol_indexes[proto].staleness.status)

    Attributes:
        workspace_root: Absolute path to the detected workspace root.
        project_type: One of ``"panther"``, ``"standalone"``, or ``"fallback"``.
        workspace_config: The full :class:`WorkspaceConfig` from detection.
        protocol_indexes: Mapping of protocol name to :class:`ProtocolIndex`.
    """

    def __init__(
        self,
        workspace_root: str,
        project_type: str,
        workspace_config: WorkspaceConfig,
        protocol_indexes: Optional[Dict[str, ProtocolIndex]] = None,
    ) -> None:
        """Initialize workspace context with root, type, and config."""
        self.workspace_root = workspace_root
        self.project_type = project_type
        self.workspace_config = workspace_config
        self.protocol_indexes: Dict[str, ProtocolIndex] = protocol_indexes or {}
        self.overlay = SessionOverlay()
        self.active_views: Dict[str, TestScopeView] = {}
        self.active_workspace: ActiveWorkspace = ActiveWorkspace.cleared()

    # -- Factory methods ----------------------------------------------------

    @classmethod
    def load(cls, start_dir: str) -> WorkspaceContext:
        """Detect workspace and load all protocol indexes.

        Args:
            start_dir: Directory to start workspace detection from.

        Returns:
            A fully populated WorkspaceContext (possibly with empty indexes
            if no ``.ivy-index/`` directories are found).
        """
        ws_config = detect_ivy_workspace(start_dir)
        project_type = ws_config.project_type or "fallback"

        ctx = cls(
            workspace_root=ws_config.workspace_root,
            project_type=project_type,
            workspace_config=ws_config,
        )

        # Glob for .ivy-index/manifest.json under protocol-testing/*/
        pattern = os.path.join(
            ws_config.workspace_root,
            "protocol-testing",
            "*",
            ".ivy-index",
            "manifest.json",
        )
        manifest_paths = glob.glob(pattern)

        for manifest_path in sorted(manifest_paths):
            index_dir = os.path.dirname(manifest_path)
            protocol_dir = os.path.dirname(index_dir)
            protocol = os.path.basename(protocol_dir)

            idx = cls._load_protocol_index(protocol, index_dir)
            if idx is not None:
                ctx.protocol_indexes[protocol] = idx
                logger.info(
                    "Loaded index for protocol %s (%s, %d symbols files)",
                    protocol,
                    idx.staleness.status,
                    len(idx.symbols),
                )

        if not ctx.protocol_indexes:
            logger.debug(
                "No .ivy-index directories found under %s",
                ws_config.workspace_root,
            )

        return ctx

    @classmethod
    def detect(cls, start_dir: str) -> dict:
        """Detect workspace and return a CLI-friendly dict.

        Suitable for JSON output in shell scripts or CLI tools.

        Args:
            start_dir: Directory to start workspace detection from.

        Returns:
            A dict with workspace metadata and staleness per protocol.
        """
        ctx = cls.load(start_dir)
        staleness_map = {}
        for proto, idx in ctx.protocol_indexes.items():
            staleness_map[proto] = idx.staleness.status

        return {
            "workspace_root": ctx.workspace_root,
            "project_type": ctx.project_type,
            "detected_by": ctx.workspace_config.detected_by,
            "protocols": sorted(ctx.protocol_indexes.keys()),
            "has_index": ctx.has_index(),
            "staleness": staleness_map,
        }

    # -- Active workspace state -------------------------------------------

    def load_active_workspace(
        self,
        state_file_path: str,
        detected_protocol_id: Optional[str] = None,
    ) -> None:
        """Load workspace state with RF-5 tiebreak logic.

        Priority rules:

        - Persisted state with ``set_by="explicit"`` ALWAYS wins over any
          auto-detection result.
        - Persisted state with ``set_by="marker"`` is overridden by a new marker
          detection **only** when the marker's ``protocol_id`` differs from the
          persisted ``active_group``.
        - No persisted state (file missing or cleared): workspace stays cleared.
          A subsequent call to the ``ivy_workspace`` MCP tool will activate it.

        Args:
            state_file_path: Path to the ``.ivy-workspace-state.json`` file.
            detected_protocol_id: The ``protocol_id`` from the freshly detected
                :class:`~ivy_lsp.workspace.detection.WorkspaceConfig`, or
                ``None`` when detection did not produce a protocol ID.
        """
        persisted = ActiveWorkspace.load(state_file_path)

        if persisted.is_set():
            if persisted.set_by == "explicit":
                # Explicit always wins over auto-detection
                self.active_workspace = persisted
                return
            if persisted.set_by == "marker" and detected_protocol_id is not None:
                if persisted.active_group != detected_protocol_id:
                    # New marker detection overrides old marker state;
                    # the workspace tool will set the correct state later.
                    self.active_workspace = ActiveWorkspace.cleared()
                    return
            self.active_workspace = persisted
            return

        # No persisted state — stay cleared
        self.active_workspace = ActiveWorkspace.cleared()

    # -- Index loading (private) -------------------------------------------

    @classmethod
    def _load_protocol_index(
        cls, protocol: str, index_dir: str
    ) -> Optional[ProtocolIndex]:
        """Load a single protocol's index from its ``.ivy-index/`` directory.

        Returns ``None`` if the manifest is missing or corrupt.
        All other artifacts are optional and default to empty on failure.
        """
        # 1. manifest.json (required)
        manifest = _load_json(os.path.join(index_dir, "manifest.json"))
        if manifest is None:
            logger.warning(
                "Skipping protocol %s: manifest.json missing or corrupt in %s",
                protocol,
                index_dir,
            )
            return None

        # 2. symbols.json (optional)
        symbols_raw = _load_json(os.path.join(index_dir, "symbols.json"))
        symbols: Dict[str, List[dict]] = (
            symbols_raw if isinstance(symbols_raw, dict) else {}
        )

        # 3. includes.json (optional)
        includes_raw = _load_json(os.path.join(index_dir, "includes.json"))
        if isinstance(includes_raw, dict):
            try:
                includes = IncludeGraph.from_edges(includes_raw)
            except Exception:
                logger.warning(
                    "Corrupt includes.json for protocol %s, using empty graph",
                    protocol,
                )
                includes = IncludeGraph()
        else:
            includes = IncludeGraph()

        # 4. exports.json (optional)
        exports_raw = _load_json(os.path.join(index_dir, "exports.json"))
        exports: Dict[str, ExportImportInfo] = {}
        if isinstance(exports_raw, dict):
            for file_key, info_dict in exports_raw.items():
                try:
                    exports[file_key] = ExportImportInfo.from_dict(info_dict)
                except Exception:
                    logger.warning(
                        "Corrupt export entry for %s in protocol %s, skipping",
                        file_key,
                        protocol,
                    )

        # 5. scopes/ directory (optional)
        scopes = _load_scopes(index_dir, protocol)

        # 6. semantic_model.pickle.gz (optional)
        semantic_model = _load_pickle(
            os.path.join(index_dir, "semantic_model.pickle.gz"),
            "semantic_model",
            protocol,
        )

        # 7. requirement_graph.pickle.gz (optional)
        requirement_graph = _load_pickle(
            os.path.join(index_dir, "requirement_graph.pickle.gz"),
            "requirement_graph",
            protocol,
        )

        # 8. Staleness check
        protocol_dir = os.path.dirname(index_dir)
        staleness = cls._check_staleness(manifest, protocol_dir)

        return ProtocolIndex(
            protocol=protocol,
            index_dir=index_dir,
            manifest=manifest,
            symbols=symbols,
            includes=includes,
            exports=exports,
            scopes=scopes,
            semantic_model=semantic_model,
            requirement_graph=requirement_graph,
            staleness=staleness,
        )

    @classmethod
    def _check_staleness(cls, manifest: dict, protocol_dir: str) -> StalenessInfo:
        """Compare manifest file mtimes against actual file mtimes.

        The manifest is expected to have a ``"files"`` key mapping file
        paths to dicts with ``"mtime"`` values.

        Returns:
            A :class:`StalenessInfo` with status fresh/stale_minor/stale_major.
        """
        files_meta = manifest.get("files", {})
        if not files_meta:
            return StalenessInfo(status="stale_major", changed_files=0, total_files=0)

        total = len(files_meta)
        changed = 0

        for rel_path, meta in files_meta.items():
            expected_mtime = meta.get("mtime") if isinstance(meta, dict) else None
            if expected_mtime is None:
                changed += 1
                continue

            abs_path = os.path.join(protocol_dir, rel_path)
            try:
                actual_mtime = os.path.getmtime(abs_path)
            except OSError:
                # File deleted or inaccessible
                changed += 1
                continue

            # Allow 1-second tolerance for filesystem mtime granularity
            if abs(actual_mtime - expected_mtime) > 1.0:
                changed += 1

        if changed == 0:
            status: Literal["fresh", "stale_minor", "stale_major"] = "fresh"
        elif total > 0 and (changed / total) < 0.10:
            status = "stale_minor"
        else:
            status = "stale_major"

        return StalenessInfo(status=status, changed_files=changed, total_files=total)

    # -- Query methods ------------------------------------------------------

    def has_index(self) -> bool:
        """Return True if any protocol index was loaded."""
        return len(self.protocol_indexes) > 0

    def list_protocols(self) -> List[str]:
        """Return sorted list of indexed protocol names."""
        return sorted(self.protocol_indexes.keys())

    def list_tests(self, protocol: Optional[str] = None) -> List[str]:
        """Return test names across protocol indexes.

        Args:
            protocol: If given, only list tests for that protocol.

        Returns:
            Sorted list of test names (scope keys).
        """
        tests: List[str] = []
        if protocol is not None:
            idx = self.protocol_indexes.get(protocol)
            if idx is not None:
                tests.extend(idx.scopes.keys())
        else:
            for idx in self.protocol_indexes.values():
                tests.extend(idx.scopes.keys())
        return sorted(tests)

    def get_test_scope(self, test_name: str) -> Optional[TestScope]:
        """Look up a test scope by name across all protocol indexes.

        Args:
            test_name: The test name (scope key).

        Returns:
            The :class:`TestScope` if found, else ``None``.
        """
        for idx in self.protocol_indexes.values():
            scope = idx.scopes.get(test_name)
            if scope is not None:
                return scope
        return None

    def create_view(self, name: str, test_name: str) -> Optional[TestScopeView]:
        """Create a scoped view for a test, returns None if scope not found.

        Args:
            name: Unique name for this view (used as key in ``active_views``).
            test_name: The test name to scope the view to.

        Returns:
            A :class:`TestScopeView` if the test scope exists, else ``None``.
        """
        scope = self.get_test_scope(test_name)
        if scope is None:
            return None
        # Determine protocol from scope
        protocol = ""
        for proto_name, proto_idx in self.protocol_indexes.items():
            if test_name in proto_idx.scopes:
                protocol = proto_name
                break
        view = TestScopeView(name, test_name, protocol, scope, self.overlay)
        self.active_views[name] = view
        return view

    def resolve_include(
        self, name: str, from_file: Optional[str] = None  # noqa: ARG002
    ) -> Optional[str]:
        """Resolve an include name to a file path using the index.

        Searches include graphs across all protocol indexes for a file
        whose basename (without extension) matches *name*.

        Args:
            name: The include name (e.g. ``"quic_types"``).
            from_file: Optional file requesting the include (unused currently
                       but reserved for future context-aware resolution).

        Returns:
            Absolute file path if resolved, else ``None``.
        """
        for idx in self.protocol_indexes.values():
            # Search through include graph edges for a matching target
            edges = idx.includes.to_edges()
            for targets in edges.values():
                for target in targets:
                    basename = os.path.splitext(os.path.basename(target))[0]
                    if basename == name:
                        return target
            # Also check symbols keys (which are file paths)
            for file_path in idx.symbols:
                basename = os.path.splitext(os.path.basename(file_path))[0]
                if basename == name:
                    return file_path
        return None


# ---------------------------------------------------------------------------
# Helper functions (module-private)
# ---------------------------------------------------------------------------


def _load_json(path: str) -> Optional[Any]:
    """Load a JSON file, returning None on any error."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to load JSON from %s: %s", path, exc)
        return None


def _load_pickle(path: str, artifact_name: str, protocol: str) -> Optional[Any]:
    """Load a gzipped pickle file, returning None on any error."""
    if not os.path.isfile(path):
        return None
    try:
        with gzip.open(path, "rb") as f:
            return pickle.load(f)  # noqa: S301
    except Exception as exc:
        logger.warning(
            "Failed to load %s pickle for protocol %s: %s",
            artifact_name,
            protocol,
            exc,
        )
        return None


def _load_scopes(index_dir: str, protocol: str) -> Dict[str, TestScope]:
    """Load test scopes from the ``scopes/`` subdirectory.

    Reads ``scopes/_meta.json`` (if present) and individual
    ``scopes/<test>.json`` files.

    Returns:
        Mapping of test name to :class:`TestScope`.
    """
    scopes_dir = os.path.join(index_dir, "scopes")
    if not os.path.isdir(scopes_dir):
        return {}

    result: Dict[str, TestScope] = {}

    # Try _meta.json first (bulk format: list of scope dicts)
    meta_path = os.path.join(scopes_dir, "_meta.json")
    meta_data = _load_json(meta_path)
    if isinstance(meta_data, list):
        for entry in meta_data:
            if not isinstance(entry, dict):
                continue
            try:
                scope = TestScope.from_dict(entry)
                test_name = entry.get(
                    "test", os.path.basename(scope.test_file).replace(".ivy", "")
                )
                result[test_name] = scope
            except Exception:
                logger.warning(
                    "Corrupt scope entry in _meta.json for protocol %s, skipping",
                    protocol,
                )

    # Also load individual <test>.json files
    scope_pattern = os.path.join(scopes_dir, "*.json")
    for scope_path in sorted(glob.glob(scope_pattern)):
        basename = os.path.basename(scope_path)
        if basename == "_meta.json":
            continue
        test_name = basename.replace(".json", "")
        if test_name in result:
            continue  # Already loaded from _meta.json

        scope_data = _load_json(scope_path)
        if isinstance(scope_data, dict):
            try:
                result[test_name] = TestScope.from_dict(scope_data)
            except Exception:
                logger.warning(
                    "Corrupt scope file %s for protocol %s, skipping",
                    scope_path,
                    protocol,
                )

    return result
