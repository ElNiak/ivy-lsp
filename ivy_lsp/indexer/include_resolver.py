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
_EXCLUDED_DIR_BASENAMES = frozenset(
    {
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
    }
)

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
        """Initialize resolver with workspace root and search paths."""
        self._workspace_root = os.path.abspath(workspace_root)
        self._ivy_include_path = ivy_include_path
        self._exclude_paths = [p.rstrip(os.sep) for p in (exclude_paths or [])]
        self._include_paths = [p.rstrip(os.sep) for p in (include_paths or [])]
        self._staging_dir: Optional[str] = None
        self._staged_files: Dict[str, str] = {}
        # Collision tracking: basename → list of all source paths that share it.
        self._collision_map: Dict[str, List[str]] = {}
        # Per-partition staging directories (populated by build_partitioned_staging).
        self._partition_staging: Dict[str, str] = {}
        # File → partition ID mapping (populated by build_partitioned_staging).
        self._file_to_partition: Dict[str, str] = {}

    @property
    def collision_map(self) -> Dict[str, List[str]]:
        """Basename → list of all source paths sharing that basename."""
        return dict(self._collision_map)

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
                os.path.join(self._workspace_root, ip) for ip in self._include_paths
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
                    d
                    for d in dirnames
                    if d not in _EXCLUDED_DIR_BASENAMES
                    and not any(
                        fnmatch.fnmatch(d, pat) for pat in _EXCLUDED_DIR_PATTERNS
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

        Also builds ``_collision_map`` tracking all basename collisions so
        downstream scope-partitioning can use the data.

        Returns:
            Absolute path to the staging directory.
        """
        staging = tempfile.mkdtemp(prefix="ivy-lsp-stage-")
        atexit.register(lambda d=staging: shutil.rmtree(d, ignore_errors=True))
        self._staging_dir = staging
        self._staged_files.clear()
        self._collision_map.clear()
        source_files = self._find_source_files()

        # First pass: build collision map (basename → all source paths).
        basename_to_paths: Dict[str, List[str]] = {}
        for filepath in source_files:
            basename = os.path.basename(filepath)
            basename_to_paths.setdefault(basename, []).append(filepath)

        # Record only actual collisions (2+ files sharing a basename).
        for basename, paths in basename_to_paths.items():
            if len(paths) > 1:
                self._collision_map[basename] = list(paths)
                logger.warning(
                    "Basename collision: %s has %d variants: %s",
                    basename,
                    len(paths),
                    [os.path.relpath(p, self._workspace_root) for p in paths],
                )

        # Second pass: create symlinks (first sorted path wins, as before).
        collisions = 0
        for filepath in source_files:
            basename = os.path.basename(filepath)
            link_path = os.path.join(staging, basename)
            if os.path.exists(link_path):
                collisions += 1
                logger.warning(
                    "Staging collision: %s (keeping %s, skipping %s)",
                    basename,
                    os.path.relpath(self._staged_files[basename], self._workspace_root),
                    os.path.relpath(filepath, self._workspace_root),
                )
                continue
            try:
                os.symlink(filepath, link_path)
            except OSError as exc:
                logger.warning("Failed to create symlink for %s: %s", filepath, exc)
                continue
            self._staged_files[basename] = filepath
        logger.info(
            "Staged %d files in %s (%d collisions, %d unique basenames affected)",
            len(self._staged_files),
            staging,
            collisions,
            len(self._collision_map),
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

    # ------------------------------------------------------------------
    # Partition-aware staging (Phase 0.2)
    # ------------------------------------------------------------------

    def build_partitioned_staging(
        self, test_scopes: Dict[str, "frozenset[str]"]
    ) -> None:
        """Build per-partition staging directories from test scope closures.

        Groups test scopes into partitions where no two scopes in the same
        partition have conflicting basenames.  Each partition gets its own
        staging subdirectory with correct symlinks.

        Files not in any collision are placed in a shared "default" partition.

        Args:
            test_scopes: Mapping of test_file → include_closure (frozenset of
                absolute file paths).
        """
        if not self._collision_map:
            # No collisions — every file can use the default staging.
            logger.info("No basename collisions — partitioned staging not needed")
            return

        if not self._staging_dir:
            logger.warning("Cannot build partitioned staging: no staging dir active")
            return

        # For each colliding basename, determine which test scopes contain
        # each variant.
        # collision_basename → {variant_path → set of test_file keys}
        basename_variant_scopes: Dict[str, Dict[str, List[str]]] = {}
        for basename, variant_paths in self._collision_map.items():
            variant_map: Dict[str, List[str]] = {}
            for variant_path in variant_paths:
                tests_using = []
                for test_file, closure in test_scopes.items():
                    if variant_path in closure:
                        tests_using.append(test_file)
                variant_map[variant_path] = tests_using
            basename_variant_scopes[basename] = variant_map

        # Assign test scopes to partitions via conflict graph coloring.
        # Two test scopes conflict if they need different files for the same
        # basename.  We use a greedy graph-coloring approach.
        #
        # test_file → partition_id (string)
        test_to_partition: Dict[str, str] = {}
        # Build conflict edges: test_a conflicts with test_b if they need
        # different variants of the same basename.
        conflict_graph: Dict[str, set] = {}
        for _basename, variant_map in basename_variant_scopes.items():
            # Group tests by which variant they use
            variant_groups = list(variant_map.values())
            for i in range(len(variant_groups)):
                for j in range(i + 1, len(variant_groups)):
                    for t_a in variant_groups[i]:
                        for t_b in variant_groups[j]:
                            conflict_graph.setdefault(t_a, set()).add(t_b)
                            conflict_graph.setdefault(t_b, set()).add(t_a)

        # Greedy coloring
        all_tests = sorted(test_scopes.keys())
        colors: Dict[str, int] = {}
        for test_file in all_tests:
            neighbor_colors = {
                colors[n] for n in conflict_graph.get(test_file, set()) if n in colors
            }
            color = 0
            while color in neighbor_colors:
                color += 1
            colors[test_file] = color

        # Assign partition IDs
        for test_file, color in colors.items():
            test_to_partition[test_file] = f"partition_{color}"

        # Tests not in any conflict get the default partition
        for test_file in all_tests:
            if test_file not in test_to_partition:
                test_to_partition[test_file] = "partition_0"

        # Build file → partition mapping
        self._file_to_partition.clear()
        for test_file, closure in test_scopes.items():
            partition_id = test_to_partition.get(test_file, "partition_0")
            for f in closure:
                # If a file belongs to multiple partitions, pick the first
                # (it's a shared module — any partition's staging works for
                # non-colliding basenames).
                if f not in self._file_to_partition:
                    self._file_to_partition[f] = partition_id

        # Create per-partition staging subdirectories
        unique_partitions = sorted(set(test_to_partition.values()))
        self._partition_staging.clear()
        for partition_id in unique_partitions:
            part_dir = os.path.join(self._staging_dir, partition_id)
            os.makedirs(part_dir, exist_ok=True)
            self._partition_staging[partition_id] = part_dir

        # Populate per-partition staging with correct symlinks
        for partition_id in unique_partitions:
            part_dir = self._partition_staging[partition_id]
            # Gather all files in scopes belonging to this partition
            partition_files: set = set()
            for test_file, pid in test_to_partition.items():
                if pid == partition_id:
                    partition_files |= test_scopes.get(test_file, frozenset())

            # Create symlinks for this partition's files
            staged_in_partition: Dict[str, str] = {}
            for filepath in sorted(partition_files):
                basename = os.path.basename(filepath)
                link_path = os.path.join(part_dir, basename)
                if basename in staged_in_partition:
                    continue  # Already staged in this partition
                try:
                    os.symlink(filepath, link_path)
                    staged_in_partition[basename] = filepath
                except OSError as exc:
                    logger.warning(
                        "Partition %s: symlink failed for %s: %s",
                        partition_id,
                        filepath,
                        exc,
                    )

        logger.info(
            "Built %d partition staging dirs (%d colliding basenames, %d test scopes)",
            len(unique_partitions),
            len(self._collision_map),
            len(test_scopes),
        )

    def resolve_partitioned(self, include_name: str, from_file: str) -> Optional[str]:
        """Resolve an include using partition-aware staging.

        If partitioned staging is active, uses the partition that *from_file*
        belongs to.  Otherwise falls back to the default resolve method.
        """
        if not self._partition_staging or not self._file_to_partition:
            return self.resolve(include_name, from_file)

        fname = include_name + ".ivy"
        abs_from = os.path.abspath(from_file)

        # 1. Same directory as the including file
        from_dir = os.path.dirname(abs_from)
        candidate = os.path.join(from_dir, fname)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

        # 2. Partition-specific staging directory
        partition_id = self._file_to_partition.get(abs_from)
        if partition_id and partition_id in self._partition_staging:
            part_dir = self._partition_staging[partition_id]
            candidate = os.path.join(part_dir, fname)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)

        # 3. Default staging directory (for files not in any partition)
        if self._staging_dir:
            candidate = os.path.join(self._staging_dir, fname)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)

        # 4. Workspace root
        candidate = os.path.join(self._workspace_root, fname)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

        # 5. Standard library
        std_dir = self._get_std_include_dir()
        if std_dir is not None:
            candidate = os.path.join(std_dir, fname)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

        return None

    def get_partition_for_file(self, filepath: str) -> Optional[str]:
        """Return the partition ID for a file, or None if unpartitioned."""
        return self._file_to_partition.get(os.path.abspath(filepath))

    def get_files_in_partition(self, partition_id: str) -> List[str]:
        """Return all files assigned to a given partition."""
        return sorted(
            f for f, pid in self._file_to_partition.items() if pid == partition_id
        )

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
