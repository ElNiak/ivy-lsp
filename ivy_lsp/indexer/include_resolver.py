"""Resolve Ivy ``include`` directives to absolute file paths."""

from __future__ import annotations

import atexit
import fnmatch
import logging
import os
import shutil
import tempfile
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Directory basenames that should be excluded from workspace scanning.
# These patterns avoid indexing build artifacts, VCS internals, and
# transient test outputs that produce noisy parse warnings.
_EXCLUDED_DIR_BASENAMES = frozenset({
    "build",
    "dist",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "submodules",
    "test",
    "include",
    "doc",
    "examples",
    "notebooks",
    "patches",
})

# Glob-style patterns matched against directory basenames.
_EXCLUDED_DIR_PATTERNS = [
    "pytest-of-*",
    "pytest-*",
]


class IncludeResolver:
    """Resolve ``include X`` to absolute file paths.

    Search order:
    1. Same directory as the including file
    2. Staging directory (flat symlinks, when active)
    3. Workspace root directory
    4. Standard library (``ivy/include/1.7/``)
    """

    def __init__(
        self,
        workspace_root: str,
        ivy_include_path: Optional[str] = None,
        exclude_paths: Optional[List[str]] = None,
        include_paths: Optional[List[str]] = None,
    ) -> None:
        self._workspace_root = os.path.abspath(workspace_root)
        self._ivy_include_path = ivy_include_path
        self._exclude_paths = [p.rstrip(os.sep) for p in (exclude_paths or [])]
        self._include_paths = [p.rstrip(os.sep) for p in (include_paths or [])]
        self._staging_dir: Optional[str] = None
        self._staged_files: Dict[str, str] = {}

    def to_config_dict(self) -> dict:
        """Serialize resolver configuration for cross-process transfer."""
        return {
            "workspace_root": self._workspace_root,
            "ivy_include_path": self._ivy_include_path,
            "exclude_paths": list(self._exclude_paths),
            "include_paths": list(self._include_paths),
            "staging_dir": self._staging_dir,
        }

    @classmethod
    def from_config(cls, d: dict) -> "IncludeResolver":
        """Restore an IncludeResolver from a config dict."""
        instance = cls(
            d["workspace_root"],
            ivy_include_path=d.get("ivy_include_path"),
            exclude_paths=d.get("exclude_paths", []),
            include_paths=d.get("include_paths", []),
        )
        instance._staging_dir = d.get("staging_dir")
        return instance

    def resolve(self, include_name: str, from_file: str) -> Optional[str]:
        """Resolve an include name to an absolute file path.

        Args:
            include_name: The bare name from ``include X`` (without .ivy).
            from_file: Absolute path of the file containing the include.

        Returns:
            Absolute path to the resolved .ivy file, or None if not found.
        """
        fname = include_name + ".ivy"

        # 1. Same directory as the including file
        from_dir = os.path.dirname(os.path.abspath(from_file))
        candidate = os.path.join(from_dir, fname)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

        # 2. Staging directory (flat, unique per basename)
        if self._staging_dir:
            candidate = os.path.join(self._staging_dir, fname)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)

        # 3. Workspace root
        candidate = os.path.join(self._workspace_root, fname)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

        # 4. Standard library
        std_dir = self._get_std_include_dir()
        if std_dir is not None:
            candidate = os.path.join(std_dir, fname)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

        return None

    def _find_source_files(self, search_root: Optional[str] = None) -> List[str]:
        """Walk the directory tree and return all .ivy file paths, sorted.

        When ``include_paths`` is set and no explicit *search_root* is given,
        only the specified subdirectories are walked.  Exclusions from
        ``exclude_paths`` and :data:`_EXCLUDED_DIR_BASENAMES` still apply
        within each included path.

        Args:
            search_root: Directory to search. Defaults to workspace_root
                (or each include_path when set).

        Returns:
            Sorted list of absolute paths to .ivy files.
        """
        # Determine which root(s) to walk.
        if search_root:
            roots = [search_root]
        elif self._include_paths:
            roots = [
                os.path.join(self._workspace_root, ip)
                for ip in self._include_paths
            ]
        else:
            roots = [self._workspace_root]

        result: List[str] = []
        for root in roots:
            if not os.path.isdir(root):
                logger.warning("Include path does not exist: %s", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded directories in-place.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _EXCLUDED_DIR_BASENAMES
                    and not any(
                        fnmatch.fnmatch(d, pat)
                        for pat in _EXCLUDED_DIR_PATTERNS
                    )
                ]
                # Path-based exclusions (relative to workspace root).
                if self._exclude_paths:
                    rel_dir = os.path.relpath(dirpath, self._workspace_root)
                    if any(
                        rel_dir == ep or rel_dir.startswith(ep + os.sep)
                        for ep in self._exclude_paths
                    ):
                        dirnames.clear()
                        continue
                for fn in filenames:
                    if fn.endswith(".ivy"):
                        result.append(os.path.join(dirpath, fn))
        if not result and self._include_paths:
            logger.error(
                "No .ivy files found: all include paths non-existent. "
                "Paths: %s, workspace: %s",
                [os.path.join(self._workspace_root, ip) for ip in self._include_paths],
                self._workspace_root,
            )
        return sorted(result)

    def find_all_ivy_files(self, root: Optional[str] = None) -> List[str]:
        """Return all .ivy file paths in the workspace, sorted.

        When a staging directory is active and *root* is ``None``, returns
        the original (dereferenced) source paths from the staging map
        instead of re-walking the filesystem.

        Args:
            root: Directory to search. Defaults to workspace_root.

        Returns:
            Sorted list of absolute paths to .ivy files.
        """
        if self._staging_dir and root is None:
            return sorted(self._staged_files.values())
        return self._find_source_files(root)

    def create_staging_directory(self) -> str:
        """Create a flat temp directory with one symlink per .ivy file.

        Mirrors how ``ivyc`` prepares ``include/1.7/`` -- a flat directory
        where each basename maps to exactly one file.  When multiple source
        files share the same basename, the first one (sorted path order) wins.

        Returns:
            Absolute path to the staging directory.
        """
        staging = tempfile.mkdtemp(prefix="ivy-lsp-stage-")
        atexit.register(lambda d=staging: shutil.rmtree(d, ignore_errors=True))
        self._staging_dir = staging
        self._staged_files.clear()
        source_files = self._find_source_files()
        collisions = 0
        for filepath in source_files:
            basename = os.path.basename(filepath)
            link_path = os.path.join(staging, basename)
            if os.path.exists(link_path):
                collisions += 1
                logger.debug(
                    "Staging collision: %s (keeping %s, skipping %s)",
                    basename,
                    self._staged_files[basename],
                    filepath,
                )
                continue
            try:
                os.symlink(filepath, link_path)
            except OSError as exc:
                logger.warning("Failed to create symlink for %s: %s", filepath, exc)
                continue
            self._staged_files[basename] = filepath
        logger.info(
            "Staged %d files in %s (%d collisions skipped)",
            len(self._staged_files),
            staging,
            collisions,
        )
        return staging

    def cleanup_staging(self) -> None:
        """Remove the staging directory and clear the staged file map."""
        if self._staging_dir and os.path.isdir(self._staging_dir):
            def _on_error(func, path, exc_info):
                logger.warning("Staging cleanup error: %s on %s", func.__name__, path)

            shutil.rmtree(self._staging_dir, onerror=_on_error)
            self._staging_dir = None
            self._staged_files.clear()

    def get_staged_path(self, filepath: str) -> Optional[str]:
        """Return the staging symlink path for a file, or None.

        Looks up *filepath*'s basename in the staging directory.  Only
        returns a path when *filepath* is the file that was actually
        staged under that basename (not a collision victim with the
        same name).  Returns None when staging is inactive, the basename
        was not staged, the file is a collision victim, or the symlink
        no longer exists on disk.
        """
        if not self._staging_dir:
            return None
        basename = os.path.basename(filepath)
        original = self._staged_files.get(basename)
        if original is None:
            return None
        if os.path.abspath(filepath) != os.path.abspath(original):
            return None
        staged = os.path.join(self._staging_dir, basename)
        if os.path.isfile(staged):
            return staged
        logger.warning(
            "Staged symlink missing for %s (expected at %s)",
            filepath,
            staged,
        )
        return None

    def _get_std_include_dir(self) -> Optional[str]:
        """Locate the Ivy standard library include directory.

        Tries the custom ``ivy_include_path`` first, then attempts to
        import ``ivy`` and locate ``ivy/include/<version>/``, selecting
        the highest version directory available.

        Returns:
            Absolute path to the standard library include directory,
            or None if not found.
        """
        if self._ivy_include_path is not None:
            return self._ivy_include_path
        try:
            import ivy as ivy_mod

            ivy_dir = os.path.dirname(os.path.abspath(ivy_mod.__file__))
            inc_base = os.path.join(ivy_dir, "include")
            if not os.path.isdir(inc_base):
                return None
            best: Optional[str] = None
            for d in os.listdir(inc_base):
                full = os.path.join(inc_base, d)
                if os.path.isdir(full) and d.replace(".", "").isdigit():
                    if best is None or d > best:
                        best = d
            if best is not None:
                return os.path.join(inc_base, best)
        except ImportError:
            logger.debug("ivy package not importable; no standard include dir")
        except (AttributeError, OSError) as exc:
            logger.warning("Failed to locate ivy standard include directory: %s", exc)
        return None
