"""Auto-detect the correct Ivy workspace scope.

Provides a multi-strategy detection pipeline that narrows the workspace
to the relevant subset of ``.ivy`` files, avoiding noisy doc/example
files and reducing indexing time.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from ivy_lsp.infra.config import get_config

logger = logging.getLogger(__name__)

_IVYWORKSPACE_FILENAME = ".ivyworkspace"


@dataclass
class WorkspaceLayer:
    """A named layer of include paths with a priority."""

    id: str
    include_paths: list[str]
    priority: int = 1
    depends_on: list[str] = field(default_factory=list)


@dataclass
class WorkspaceConfig:
    """Result of workspace detection (v3 schema).

    The ``scope_detection`` field controls how endpoint mirror scope
    partitions are computed:

    - ``"auto"`` (default): partitions are dynamically derived from the
      include graph after indexing.  No manual configuration needed.
    - ``"explicit"``: reserved for future manual partition hints.
    """

    workspace_root: str
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    detected_by: str = "fallback"  # explicit, marker, hint, heuristic, fallback
    project_type: Optional[str] = None  # panther, standalone, None
    scope_detection: str = "auto"  # auto, explicit
    standard_library: Optional[str] = None  # e.g. "ivy/include/1.7"
    workspace_layers: list[WorkspaceLayer] = field(default_factory=list)
    workspace_groups: dict[str, list[str]] = field(default_factory=dict)
    protocol_id: Optional[str] = None
    workspace_root_offset: Optional[str] = None


def _read_marker(marker_path: str) -> Optional[dict]:
    """Read and validate an ``.ivyworkspace`` JSON marker file."""
    try:
        with open(marker_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Invalid .ivyworkspace (not a JSON object): %s", marker_path)
            return None
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read .ivyworkspace at %s: %s", marker_path, exc)
        return None


def _apply_marker(marker_path: str, data: dict) -> Optional[WorkspaceConfig]:
    """Build a WorkspaceConfig from a parsed marker file (v3 schema).

    Returns ``None`` for unsupported v1/v2 markers instead of crashing.
    """
    marker_dir = os.path.dirname(os.path.abspath(marker_path))
    version = data.get("version", 1)

    if version < 3:
        logger.warning(
            ".ivyworkspace v%d at %s is no longer supported; ignoring. "
            "Upgrade to v3 with workspace_layers.",
            version,
            marker_path,
        )
        return None

    # Parse workspace_layers
    raw_layers = data.get("workspace_layers", [])
    layers = [
        WorkspaceLayer(
            id=layer["id"],
            include_paths=layer.get("include_paths", []),
            priority=layer.get("priority", 1),
            depends_on=layer.get("depends_on", []),
        )
        for layer in raw_layers
    ]

    # Flatten all layer include_paths into a single list for _find_source_files
    flat_include_paths = []
    for layer in layers:
        flat_include_paths.extend(layer.include_paths)

    # Resolve workspace_root: apply offset relative to marker_dir when present
    workspace_root_offset = data.get("workspace_root_offset")
    if workspace_root_offset is not None:
        workspace_root = os.path.normpath(
            os.path.join(marker_dir, workspace_root_offset)
        )
    else:
        workspace_root = marker_dir

    # Parse new optional fields
    workspace_groups = data.get("workspace_groups", {})
    protocol_id = data.get("protocol_id")

    return WorkspaceConfig(
        workspace_root=workspace_root,
        include_paths=flat_include_paths or data.get("include_paths", []),
        exclude_paths=data.get("exclude_paths", []),
        detected_by="marker",
        project_type=data.get("project_type"),
        scope_detection=data.get("scope_detection", "auto"),
        standard_library=data.get("standard_library"),
        workspace_layers=layers,
        workspace_groups=workspace_groups,
        protocol_id=protocol_id,
        workspace_root_offset=workspace_root_offset,
    )


def _walk_up_for_marker(
    start_dir: str, max_depth: int = 10
) -> Optional[WorkspaceConfig]:
    """Walk up from *start_dir* looking for ``.ivyworkspace``."""
    current = os.path.abspath(start_dir)
    for _ in range(max_depth):
        candidate = os.path.join(current, _IVYWORKSPACE_FILENAME)
        if os.path.isfile(candidate):
            data = _read_marker(candidate)
            if data is not None:
                config = _apply_marker(candidate, data)
                if config is not None:
                    return config
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _walk_down_for_marker(
    start_dir: str, max_depth: int = 6
) -> Optional[WorkspaceConfig]:
    """Walk down up to *max_depth* levels looking for ``.ivyworkspace``."""
    start = os.path.abspath(start_dir)
    for dirpath, dirnames, filenames in os.walk(start):
        rel = os.path.relpath(dirpath, start)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames.clear()
            continue
        # Skip hidden dirs and common noise
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if _IVYWORKSPACE_FILENAME in filenames:
            candidate = os.path.join(dirpath, _IVYWORKSPACE_FILENAME)
            data = _read_marker(candidate)
            if data is not None:
                config = _apply_marker(candidate, data)
                if config is not None:
                    # Skip sub-workspace markers embedded in a larger project.
                    if config.workspace_root_offset is not None:
                        resolved = os.path.realpath(config.workspace_root)
                        if resolved != os.path.realpath(start):
                            logger.debug(
                                "Skipping sub-workspace marker at %s "
                                "(resolved root %s != start %s)",
                                candidate,
                                resolved,
                                start,
                            )
                            continue
                    return config
    return None


def _resolve_git_worktree(start_dir: str) -> Optional[str]:
    """If *start_dir* is inside a git worktree, return the main working tree root.

    Git worktrees store a **file** at ``.git`` (not a directory) containing::

        gitdir: /path/to/main-repo/.git/worktrees/<name>

    From that gitdir, reading the ``commondir`` file gives the path to the
    main ``.git`` directory, whose parent is the main working tree.
    """
    current = os.path.realpath(os.path.abspath(start_dir))
    for _ in range(10):
        git_path = os.path.join(current, ".git")
        if os.path.isfile(git_path):
            try:
                with open(git_path) as f:
                    content = f.read().strip()
                if not content.startswith("gitdir:"):
                    break
                gitdir = content[len("gitdir:") :].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.join(current, gitdir)
                gitdir = os.path.normpath(gitdir)
                commondir_file = os.path.join(gitdir, "commondir")
                if not os.path.isfile(commondir_file):
                    break
                with open(commondir_file) as f:
                    commondir = f.read().strip()
                if not os.path.isabs(commondir):
                    commondir = os.path.normpath(os.path.join(gitdir, commondir))
                main_root = os.path.dirname(commondir)
                main_root = os.path.realpath(main_root)
                if os.path.isdir(main_root) and main_root != current:
                    logger.debug(
                        "Resolved git worktree %s -> main tree %s",
                        current,
                        main_root,
                    )
                    return main_root
            except (OSError, ValueError) as exc:
                logger.debug("Failed to resolve git worktree: %s", exc)
            break
        elif os.path.isdir(git_path):
            break  # Regular repo, not a worktree
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _discover_protocols(protocol_testing_dir: str) -> list[str]:
    """Discover protocols by scanning for per-protocol ``.ivyworkspace`` markers.

    Returns a sorted list of protocol names (subdirectory names under
    *protocol_testing_dir*) that contain a ``.ivyworkspace`` file.

    Falls back to an empty list if *protocol_testing_dir* does not exist.
    """
    protocols = []
    if not os.path.isdir(protocol_testing_dir):
        return protocols
    for entry in sorted(os.listdir(protocol_testing_dir)):
        entry_path = os.path.join(protocol_testing_dir, entry)
        if os.path.isdir(entry_path):
            marker = os.path.join(entry_path, _IVYWORKSPACE_FILENAME)
            if os.path.exists(marker):
                protocols.append(entry)
    return protocols


def _build_panther_workspace(
    panther_ivy_root: str,
) -> Optional[WorkspaceConfig]:
    """Build a WorkspaceConfig for a PANTHER panther_ivy root directory.

    Reads per-protocol ``.ivyworkspace`` markers and merges their
    fine-grained layer definitions into a single config. Protocols whose
    markers fail to parse, resolve to a different root, or have layer ID
    collisions are skipped with a warning.
    """
    protocol_testing_dir = os.path.join(panther_ivy_root, "protocol-testing")
    discovered = _discover_protocols(protocol_testing_dir)

    if not discovered:
        logger.debug(
            "No per-protocol .ivyworkspace markers found under %s; "
            "no workspace detected",
            protocol_testing_dir,
        )
        return None

    panther_ivy_real = os.path.realpath(panther_ivy_root)
    merged_layers: list[WorkspaceLayer] = []
    merged_include_paths: list[str] = []
    merged_exclude_paths: set[str] = set()
    merged_groups: dict[str, list[str]] = {}
    standard_library: Optional[str] = None
    seen_layer_ids: set[str] = set()
    any_marker_merged = False

    for protocol in discovered:
        marker_path = os.path.join(
            protocol_testing_dir, protocol, _IVYWORKSPACE_FILENAME
        )
        data = _read_marker(marker_path)
        if data is None:
            logger.warning(
                "Skipping protocol %s: failed to read .ivyworkspace", protocol
            )
            continue

        config = _apply_marker(marker_path, data)
        if config is None:
            logger.warning("Skipping protocol %s: unsupported marker version", protocol)
            continue

        # Verify resolved root matches panther_ivy_root
        resolved_root = os.path.realpath(config.workspace_root)
        if resolved_root != panther_ivy_real:
            logger.warning(
                "Skipping protocol %s: resolved root %s != expected %s",
                protocol,
                resolved_root,
                panther_ivy_real,
            )
            continue

        # Check for layer ID collisions
        new_ids = {layer.id for layer in config.workspace_layers}
        collisions = new_ids & seen_layer_ids
        if collisions:
            logger.warning(
                "Skipping protocol %s: layer ID collision with already-merged "
                "layers: %s",
                protocol,
                sorted(collisions),
            )
            continue

        # Merge this protocol's layers
        seen_layer_ids.update(new_ids)
        merged_layers.extend(config.workspace_layers)
        merged_include_paths.extend(config.include_paths)
        merged_exclude_paths.update(config.exclude_paths)
        merged_groups.update(config.workspace_groups)

        if standard_library is None and config.standard_library:
            standard_library = config.standard_library
        elif config.standard_library and config.standard_library != standard_library:
            logger.warning(
                "Protocol %s declares standard_library=%s, "
                "but %s was already selected; keeping first",
                protocol,
                config.standard_library,
                standard_library,
            )

        any_marker_merged = True

    if not any_marker_merged:
        logger.debug(
            "No valid v3 markers found under %s; no workspace detected",
            protocol_testing_dir,
        )
        return None

    return WorkspaceConfig(
        workspace_root=panther_ivy_root,
        workspace_layers=merged_layers,
        include_paths=merged_include_paths,
        exclude_paths=sorted(merged_exclude_paths),
        detected_by="heuristic+marker",
        project_type="panther",
        standard_library=standard_library,
        workspace_groups=merged_groups,
    )


def _panther_heuristic(start_dir: str) -> Optional[WorkspaceConfig]:
    """Detect PANTHER project by looking for ``protocol-testing/`` with ``.ivy`` files.

    Protocols are discovered dynamically via per-protocol ``.ivyworkspace``
    markers under ``protocol-testing/`` using :func:`_discover_protocols`.
    Returns ``None`` when no markers are found.
    """
    current = os.path.abspath(start_dir)
    for _ in range(10):
        candidate = os.path.join(
            current, "panther", "plugins", "services", "testers", "panther_ivy"
        )
        if os.path.isdir(os.path.join(candidate, "protocol-testing")):
            return _build_panther_workspace(candidate)
        # Maybe CWD is inside panther_ivy
        if os.path.isdir(os.path.join(current, "protocol-testing")) and os.path.isfile(
            os.path.join(current, "panther_ivy.py")
        ):
            return _build_panther_workspace(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def detect_ivy_workspace(
    start_dir: str,
    explicit_workspace: Optional[str] = None,
    explicit_include_paths: Optional[List[str]] = None,
    explicit_exclude_paths: Optional[List[str]] = None,
) -> WorkspaceConfig:
    """Detect the correct Ivy workspace scope.

    Detection priority:
        1. Explicit ``--workspace`` / ``IVY_LSP_WORKSPACE`` env var
        2. ``IVY_LSP_WORKSPACE_HINT`` env var → check for marker or PANTHER heuristic
        3. Walk up from *start_dir* looking for ``.ivyworkspace``
        4. Walk down 6 levels looking for ``.ivyworkspace``
        5. PANTHER heuristic (``protocol-testing/`` directory)
        5.5. Git worktree resolution → re-run detection on main working tree
        6. Fallback: use *start_dir* as-is

    Args:
        start_dir: Starting directory (typically CWD or --workspace value).
        explicit_workspace: Value from ``--workspace`` CLI flag.
        explicit_include_paths: Pre-set include paths (from env or CLI).
        explicit_exclude_paths: Pre-set exclude paths (from env or CLI).

    Returns:
        A WorkspaceConfig with the detected workspace root and paths.
    """
    # 1. Explicit workspace
    ws = explicit_workspace or get_config().workspace
    if ws:
        ws = os.path.realpath(os.path.abspath(ws))
        logger.info("Using explicit workspace: %s", ws)
        # Still honour .ivyworkspace marker if present at the explicit root
        marker_path = os.path.join(ws, _IVYWORKSPACE_FILENAME)
        logger.debug(
            "Checking explicit workspace marker: %s (exists=%s)",
            marker_path,
            os.path.isfile(marker_path),
        )
        if os.path.isfile(marker_path):
            data = _read_marker(marker_path)
            if data is not None:
                config = _apply_marker(marker_path, data)
                if config is not None:
                    config.detected_by = "explicit+marker"
                    # Explicit CLI paths take precedence over marker
                    if explicit_include_paths:
                        config.include_paths = explicit_include_paths
                    if explicit_exclude_paths:
                        config.exclude_paths = explicit_exclude_paths
                    return config
        return WorkspaceConfig(
            workspace_root=ws,
            include_paths=explicit_include_paths or [],
            exclude_paths=explicit_exclude_paths or [],
            detected_by="explicit",
        )

    abs_start = os.path.realpath(os.path.abspath(start_dir))

    # 2. Workspace hint env var
    hint = get_config().workspace_hint
    if hint:
        hint_path = os.path.join(abs_start, hint) if not os.path.isabs(hint) else hint
        marker_path = os.path.join(hint_path, _IVYWORKSPACE_FILENAME)
        if os.path.isfile(marker_path):
            data = _read_marker(marker_path)
            if data is not None:
                config = _apply_marker(marker_path, data)
                if config is not None:
                    config.detected_by = "hint"
                    logger.info(
                        "Workspace detected via hint: %s", config.workspace_root
                    )
                    return config
        elif os.path.isdir(hint_path):
            config = _panther_heuristic(hint_path)
            if config is not None:
                config.detected_by = "hint"
                logger.info(
                    "Workspace detected via hint heuristic: %s",
                    config.workspace_root,
                )
                return config
        logger.debug("Workspace hint %s did not resolve to a workspace", hint)

    # 3. Walk up for .ivyworkspace
    config = _walk_up_for_marker(abs_start)
    if config is not None:
        logger.info("Workspace detected via walk-up marker: %s", config.workspace_root)
        return config

    # 4. Walk down for .ivyworkspace
    config = _walk_down_for_marker(abs_start)
    if config is not None:
        logger.info(
            "Workspace detected via walk-down marker: %s", config.workspace_root
        )
        return config

    # 5. PANTHER heuristic
    config = _panther_heuristic(abs_start)
    if config is not None:
        logger.info(
            "Workspace detected via PANTHER heuristic: %s", config.workspace_root
        )
        return config

    # 5.5. Git worktree resolution: try the main working tree
    main_tree = _resolve_git_worktree(abs_start)
    if main_tree is not None:
        config = _walk_down_for_marker(main_tree, max_depth=6)
        if config is None:
            config = _panther_heuristic(main_tree)
        if config is not None:
            config.detected_by = f"worktree+{config.detected_by}"
            logger.info(
                "Workspace detected via worktree -> %s: %s",
                config.detected_by,
                config.workspace_root,
            )
            return config

    # 6. Fallback
    logger.info("No workspace markers found, using start directory: %s", abs_start)
    return WorkspaceConfig(
        workspace_root=abs_start,
        detected_by="fallback",
    )
