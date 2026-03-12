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

logger = logging.getLogger(__name__)

_IVYWORKSPACE_FILENAME = ".ivyworkspace"


@dataclass
class WorkspaceConfig:
    """Result of workspace detection."""

    workspace_root: str
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    detected_by: str = "fallback"  # explicit, marker, hint, heuristic, fallback
    project_type: Optional[str] = None  # panther, standalone, None


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


def _apply_marker(marker_path: str, data: dict) -> WorkspaceConfig:
    """Build a WorkspaceConfig from a parsed marker file."""
    marker_dir = os.path.dirname(os.path.abspath(marker_path))
    return WorkspaceConfig(
        workspace_root=marker_dir,
        include_paths=data.get("include_paths", []),
        exclude_paths=data.get("exclude_paths", []),
        detected_by="marker",
        project_type=data.get("project_type"),
    )


def _walk_up_for_marker(start_dir: str, max_depth: int = 10) -> Optional[WorkspaceConfig]:
    """Walk up from *start_dir* looking for ``.ivyworkspace``."""
    current = os.path.abspath(start_dir)
    for _ in range(max_depth):
        candidate = os.path.join(current, _IVYWORKSPACE_FILENAME)
        if os.path.isfile(candidate):
            data = _read_marker(candidate)
            if data is not None:
                return _apply_marker(candidate, data)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _walk_down_for_marker(start_dir: str, max_depth: int = 6) -> Optional[WorkspaceConfig]:
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
                return _apply_marker(candidate, data)
    return None


def _resolve_git_worktree(start_dir: str) -> Optional[str]:
    """If *start_dir* is inside a git worktree, return the main working tree root.

    Git worktrees store a **file** at ``.git`` (not a directory) containing::

        gitdir: /path/to/main-repo/.git/worktrees/<name>

    From that gitdir, reading the ``commondir`` file gives the path to the
    main ``.git`` directory, whose parent is the main working tree.
    """
    current = os.path.abspath(start_dir)
    for _ in range(10):
        git_path = os.path.join(current, ".git")
        if os.path.isfile(git_path):
            try:
                with open(git_path) as f:
                    content = f.read().strip()
                if not content.startswith("gitdir:"):
                    break
                gitdir = content[len("gitdir:"):].strip()
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


def _panther_heuristic(start_dir: str) -> Optional[WorkspaceConfig]:
    """Detect PANTHER project by looking for ``protocol-testing/`` with ``.ivy`` files."""
    current = os.path.abspath(start_dir)
    for _ in range(10):
        candidate = os.path.join(
            current, "panther", "plugins", "services", "testers", "panther_ivy"
        )
        if os.path.isdir(os.path.join(candidate, "protocol-testing")):
            return WorkspaceConfig(
                workspace_root=candidate,
                include_paths=["protocol-testing"],
                exclude_paths=["submodules", "test", "doc", "examples", "notebooks", "patches"],
                detected_by="heuristic",
                project_type="panther",
            )
        # Maybe CWD is inside panther_ivy
        if os.path.isdir(os.path.join(current, "protocol-testing")) and os.path.isfile(
            os.path.join(current, "panther_ivy.py")
        ):
            return WorkspaceConfig(
                workspace_root=current,
                include_paths=["protocol-testing"],
                exclude_paths=["submodules", "test", "doc", "examples", "notebooks", "patches"],
                detected_by="heuristic",
                project_type="panther",
            )
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
    ws = explicit_workspace or os.environ.get("IVY_LSP_WORKSPACE")
    if ws:
        ws = os.path.abspath(ws)
        logger.info("Using explicit workspace: %s", ws)
        return WorkspaceConfig(
            workspace_root=ws,
            include_paths=explicit_include_paths or [],
            exclude_paths=explicit_exclude_paths or [],
            detected_by="explicit",
        )

    abs_start = os.path.abspath(start_dir)

    # 2. Workspace hint env var
    hint = os.environ.get("IVY_LSP_WORKSPACE_HINT")
    if hint:
        hint_path = os.path.join(abs_start, hint) if not os.path.isabs(hint) else hint
        marker_path = os.path.join(hint_path, _IVYWORKSPACE_FILENAME)
        if os.path.isfile(marker_path):
            data = _read_marker(marker_path)
            if data is not None:
                config = _apply_marker(marker_path, data)
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
        logger.info("Workspace detected via walk-down marker: %s", config.workspace_root)
        return config

    # 5. PANTHER heuristic
    config = _panther_heuristic(abs_start)
    if config is not None:
        logger.info("Workspace detected via PANTHER heuristic: %s", config.workspace_root)
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
