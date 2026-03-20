"""Resolve Ivy ``include`` directives to absolute file paths."""

from __future__ import annotations

import atexit
import fnmatch
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

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

_STALE_THRESHOLD_SECS = 3600  # 1 hour

# Hardcoded fallback when stdlib directory cannot be discovered from disk.
# Should approximate the full set of modules in ivy/include/1.7/.
_STDLIB_FALLBACK = frozenset(
    {
        "order",
        "collections",
        "collections_impl",
        "ip",
        "ipv6",
        "tcp",
        "tcp_impl",
        "udp",
        "udp_impl",
        "byte_stream",
        "timeout",
        "net",
        "tls",
        "tls_msg",
        "serdes",
        "deserializer",
        "c_time",
        "chrono_time",
    }
)


def discover_stdlib_modules(
    ivy_include_path: Optional[str] = None, preferred_version: str = "1.7"
) -> frozenset:
    """Scan the Ivy stdlib directory and return all module basenames.

    Args:
        ivy_include_path: Explicit path to stdlib directory.  If *None*,
            auto-discovers from the installed ``ivy`` package.
        preferred_version: Preferred version subdirectory (default ``"1.7"``).
            Falls back to the highest version if preferred version is absent.

    Returns:
        frozenset of module basenames (e.g. ``{"order", "collections", "tls", ...}``).
    """
    std_dir = ivy_include_path
    if std_dir is None:
        try:
            import ivy as ivy_mod

            ivy_dir = os.path.dirname(os.path.realpath(ivy_mod.__file__))
            inc_base = os.path.join(ivy_dir, "include")
            if not os.path.isdir(inc_base):
                return _STDLIB_FALLBACK
            # Prefer specified version, fall back to highest
            preferred = os.path.join(inc_base, preferred_version)
            if os.path.isdir(preferred):
                std_dir = preferred
            else:
                best = None
                for d in os.listdir(inc_base):
                    full = os.path.join(inc_base, d)
                    if os.path.isdir(full) and d.replace(".", "").isdigit():
                        if best is None or d > best:
                            best = d
                if best:
                    std_dir = os.path.join(inc_base, best)
        except (ImportError, AttributeError, OSError):
            return _STDLIB_FALLBACK

    if not std_dir or not os.path.isdir(std_dir):
        return _STDLIB_FALLBACK

    modules = set()
    for f in os.listdir(std_dir):
        if f.endswith(".ivy"):
            modules.add(f[:-4])
    return frozenset(modules) if modules else _STDLIB_FALLBACK


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
        workspace_layers: Optional[List] = None,
    ) -> None:
        """Initialize resolver with workspace root and search paths."""
        self._workspace_root = os.path.realpath(workspace_root)
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
        self._workspace_layers = workspace_layers or []
        # File → layer ID mapping (populated when workspace_layers is set)
        self._file_to_layer: Dict[str, str] = {}
        # Layer ID → layer object mapping (populated by build_layered_staging)
        self._layer_by_id: Dict[str, Any] = {}
        # Track files we've already warned about (dedup layer routing warnings)
        self._warned_routing_miss: set = set()

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
            "workspace_layers": [
                {
                    "id": l.id,
                    "include_paths": l.include_paths,
                    "priority": l.priority,
                    "depends_on": l.depends_on,
                }
                for l in self._workspace_layers
            ],
        }

    @classmethod
    def from_config(cls, d: dict) -> "IncludeResolver":
        """Restore an IncludeResolver from a config dict."""
        from ivy_lsp.workspace_detection import WorkspaceLayer

        layers = [
            WorkspaceLayer(
                id=l["id"],
                include_paths=l.get("include_paths", []),
                priority=l.get("priority", 1),
                depends_on=l.get("depends_on", []),
            )
            for l in d.get("workspace_layers", [])
        ]
        instance = cls(
            d["workspace_root"],
            ivy_include_path=d.get("ivy_include_path"),
            exclude_paths=d.get("exclude_paths", []),
            include_paths=d.get("include_paths", []),
            workspace_layers=layers,
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
        from_file_real = os.path.realpath(from_file)

        # 1. Same directory as the including file
        from_dir = os.path.dirname(from_file_real)
        candidate = os.path.join(from_dir, fname)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)

        # 2. Layer-aware staging (when active, replaces flat staging for
        #    colliding basenames)
        if self._partition_staging and self._file_to_partition:
            abs_from = from_file_real
            layer_id = self._file_to_partition.get(abs_from)
            if layer_id and layer_id in self._partition_staging:
                candidate = os.path.join(self._partition_staging[layer_id], fname)
                if os.path.isfile(candidate):
                    return os.path.realpath(candidate)
                # Layer found but file not in layer staging dir
                logger.warning(
                    "Layer staging miss: '%s' not found in layer '%s' dir %s (from %s)",
                    fname,
                    layer_id,
                    self._partition_staging[layer_id],
                    os.path.relpath(from_file, self._workspace_root),
                )
                # Cross-layer dependency fallback: try dependency layers
                layer_obj = self._layer_by_id.get(layer_id)
                if layer_obj and layer_obj.depends_on:
                    for dep_id in layer_obj.depends_on:
                        dep_staging = self._partition_staging.get(dep_id)
                        if dep_staging:
                            candidate = os.path.join(dep_staging, fname)
                            if os.path.isfile(candidate):
                                return os.path.realpath(candidate)

                # Broader priority-ordered layer fallback with proximity scoring
                cross_candidates = []
                for lid in self._partition_staging:
                    if lid == layer_id:
                        continue
                    cand = os.path.join(self._partition_staging[lid], fname)
                    if os.path.isfile(cand):
                        cross_candidates.append((lid, cand))

                if cross_candidates:
                    if len(cross_candidates) == 1:
                        _lid, _cand = cross_candidates[0]
                        logger.debug(
                            "Cross-layer resolve: '%s' found in layer '%s' (from layer '%s')",
                            include_name,
                            _lid,
                            layer_id,
                        )
                        return os.path.realpath(_cand)

                    # Multiple candidates: score by path proximity to from_file
                    def _proximity(item):
                        _item_lid, _item_cand = item
                        real = os.path.realpath(_item_cand)
                        common = os.path.commonpath([from_file_real, real])
                        return len(common)

                    best_lid, best_cand = max(cross_candidates, key=_proximity)
                    logger.debug(
                        "Cross-layer resolve (proximity): '%s' -> layer '%s' "
                        "(from layer '%s', %d candidates)",
                        include_name,
                        best_lid,
                        layer_id,
                        len(cross_candidates),
                    )
                    return os.path.realpath(best_cand)

            elif not layer_id and self._file_to_layer:
                # File should be in a layer but isn't in _file_to_partition
                # Log WARNING on first occurrence per file, then DEBUG
                _rel = os.path.relpath(from_file, self._workspace_root)
                if _rel not in self._warned_routing_miss:
                    self._warned_routing_miss.add(_rel)
                    logger.warning(
                        "Layer routing miss: %s not in _file_to_partition (%d entries). "
                        "abs_from=%s",
                        _rel,
                        len(self._file_to_partition),
                        abs_from[-80:],
                    )
                else:
                    logger.debug("Layer routing miss (repeat): %s", _rel)

        # 3. Flat staging directory
        if self._staging_dir:
            candidate = os.path.join(self._staging_dir, fname)
            if os.path.isfile(candidate):
                basename = os.path.basename(fname)
                # Refuse to resolve colliding basenames via flat staging
                # when layers are active — the correct variant can only be
                # determined through layer routing (step 2).
                # Fall through to workspace root / stdlib as last resort.
                if self._file_to_layer and basename in self._collision_map:
                    logger.warning(
                        "Ambiguous include '%s' from %s: basename has %d variants "
                        "across layers — skipping flat staging, trying workspace root",
                        include_name,
                        os.path.relpath(from_file, self._workspace_root),
                        len(self._collision_map[basename]),
                    )
                else:
                    return os.path.realpath(candidate)

        # 4. Workspace root
        candidate = os.path.join(self._workspace_root, fname)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)

        # 5. Standard library
        std_dir = self._get_std_include_dir()
        if std_dir is not None:
            candidate = os.path.join(std_dir, fname)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)

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
        # Layer-aware discovery when v3 workspace layers are configured.
        if self._workspace_layers and search_root is None:
            layer_files = self._find_source_files_by_layer()
            all_files = []
            for files in layer_files.values():
                all_files.extend(files)
            return sorted(set(all_files))

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
        # When layer staging is active, return ALL layer-mapped files.
        # Flat _staged_files only has collision winners; layer staging
        # correctly maps every file to its layer.
        if self._file_to_layer and root is None:
            return sorted(self._file_to_layer.keys())
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
        # Cleanup stale staging directories
        tmpdir = tempfile.gettempdir()
        now = time.time()
        for entry in os.scandir(tmpdir):
            if entry.name.startswith("ivy-lsp-stage-") and entry.is_dir(
                follow_symlinks=False
            ):
                try:
                    if now - entry.stat().st_mtime > _STALE_THRESHOLD_SECS:
                        shutil.rmtree(entry.path, ignore_errors=True)
                        logger.debug("Cleaned stale staging dir: %s", entry.name)
                except OSError:
                    pass

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
            if len(paths) <= 1:
                continue
            self._collision_map[basename] = list(paths)
            # Classify: intra-layer (real problem) vs cross-layer (expected)
            if self._file_to_layer:
                layers_involved = {
                    self._file_to_layer.get(os.path.realpath(p), "unknown")
                    for p in paths
                }
                if len(layers_involved) <= 1:
                    logger.warning(
                        "Intra-layer collision: %s has %d variants in layer '%s': %s "
                        "— this MUST be fixed (duplicate basenames within same protocol)",
                        basename,
                        len(paths),
                        next(iter(layers_involved)),
                        [os.path.relpath(p, self._workspace_root) for p in paths],
                    )
                else:
                    logger.debug(
                        "Cross-layer collision (expected): %s spans layers %s",
                        basename,
                        sorted(layers_involved),
                    )
            else:
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
            if os.path.lexists(link_path):
                collisions += 1
                if self._file_to_layer:
                    # Cross-layer collisions handled by layer staging — audit only
                    logger.debug(
                        "Staging collision (layer-handled): %s (keeping %s, skipping %s)",
                        basename,
                        os.path.relpath(
                            self._staged_files[basename], self._workspace_root
                        ),
                        os.path.relpath(filepath, self._workspace_root),
                    )
                else:
                    # No layers → collision is a real ambiguity problem
                    logger.warning(
                        "Staging collision (ambiguous): %s (keeping %s, skipping %s) "
                        "— include resolution for this basename may be wrong",
                        basename,
                        os.path.relpath(
                            self._staged_files[basename], self._workspace_root
                        ),
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
        if os.path.realpath(filepath) != os.path.realpath(original):
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
        # Layer staging takes precedence — do not overwrite layer partitions
        # with scope-based partitions (the two systems are incompatible).
        if self._workspace_layers and self._partition_staging:
            logger.info(
                "Skipping scope-based partitioned staging: layer staging active "
                "(%d layers, %d files)",
                len(self._partition_staging),
                len(self._file_to_partition),
            )
            return

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
            # Clean stale symlinks before repopulating
            for entry in os.scandir(part_dir):
                if entry.is_symlink():
                    os.unlink(entry.path)
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
                if os.path.lexists(link_path):
                    os.unlink(link_path)
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
        abs_from = os.path.realpath(from_file)

        # 1. Same directory as the including file
        from_dir = os.path.dirname(abs_from)
        candidate = os.path.join(from_dir, fname)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)

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
            return os.path.realpath(candidate)

        # 5. Standard library
        std_dir = self._get_std_include_dir()
        if std_dir is not None:
            candidate = os.path.join(std_dir, fname)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)

        return None

    def get_partition_for_file(self, filepath: str) -> Optional[str]:
        """Return the partition ID for a file, or None if unpartitioned."""
        return self._file_to_partition.get(os.path.realpath(filepath))

    def get_files_in_partition(self, partition_id: str) -> List[str]:
        """Return all files assigned to a given partition."""
        return sorted(
            f for f, pid in self._file_to_partition.items() if pid == partition_id
        )

    # ------------------------------------------------------------------
    # Layer-aware staging (v3 workspace layers)
    # ------------------------------------------------------------------

    def _find_source_files_by_layer(self) -> Dict[str, List[str]]:
        """Walk each layer's include paths independently and return files grouped by layer.

        Returns:
            Mapping of layer_id -> list of absolute .ivy file paths.
        """
        layer_files: Dict[str, List[str]] = {}
        for layer in self._workspace_layers:
            roots = [
                os.path.join(self._workspace_root, ip) for ip in layer.include_paths
            ]
            files: List[str] = []
            for root in roots:
                if not os.path.isdir(root):
                    logger.warning(
                        "Layer '%s' include path does not exist: %s",
                        layer.id,
                        root,
                    )
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [
                        d
                        for d in dirnames
                        if d not in _EXCLUDED_DIR_BASENAMES
                        and not any(
                            fnmatch.fnmatch(d, pat) for pat in _EXCLUDED_DIR_PATTERNS
                        )
                    ]
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
                            filepath = os.path.realpath(os.path.join(dirpath, fn))
                            files.append(filepath)
                            self._file_to_layer[filepath] = layer.id
            layer_files[layer.id] = sorted(files)
        return layer_files

    def _validate_layer_deps(self) -> bool:
        """Check workspace layer dependencies for cycles using DFS.

        Returns:
            True if no cycles detected, False otherwise.
        """
        layer_ids = {l.id for l in self._workspace_layers}
        # WHITE=0, GRAY=1, BLACK=2
        color: Dict[str, int] = {lid: 0 for lid in layer_ids}
        dep_map: Dict[str, List[str]] = {
            l.id: l.depends_on for l in self._workspace_layers
        }

        def _dfs(node: str, path: List[str]) -> bool:
            color[node] = 1  # GRAY — visiting
            for dep in dep_map.get(node, []):
                if dep not in layer_ids:
                    logger.warning(
                        "Layer '%s' depends_on unknown layer '%s' — skipping",
                        node,
                        dep,
                    )
                    continue
                if color[dep] == 1:  # Back edge → cycle
                    cycle = path[path.index(dep) :] + [dep]
                    logger.error(
                        "Cycle detected in layer dependencies: %s",
                        " -> ".join(cycle),
                    )
                    return False
                if color[dep] == 0:
                    if not _dfs(dep, path + [dep]):
                        return False
            color[node] = 2  # BLACK — done
            return True

        for lid in layer_ids:
            if color[lid] == 0:
                if not _dfs(lid, [lid]):
                    return False
        return True

    def build_layered_staging(self) -> None:
        """Build per-layer staging directories when workspace layers are configured.

        Each layer gets its own staging subdirectory. Collisions within a layer
        use first-wins semantics. Collisions across layers are expected (e.g.,
        APT and standard both have types.ivy) and don't conflict because they're
        in separate staging dirs.

        When a layer declares ``depends_on``, files from the dependency layers
        are injected as symlinks into the dependent layer's staging directory.
        The layer's own files take precedence over injected files.
        """
        if not self._workspace_layers:
            logger.info("No workspace layers -- layered staging not needed")
            return

        if not self._staging_dir:
            logger.warning("Cannot build layered staging: no staging dir active")
            return

        # Validate dependency graph before proceeding
        has_deps = any(l.depends_on for l in self._workspace_layers)
        deps_valid = True
        if has_deps:
            deps_valid = self._validate_layer_deps()
            if not deps_valid:
                logger.error(
                    "Layer dependency cycle detected — "
                    "dependency injection will be skipped"
                )

        layer_files = self._find_source_files_by_layer()

        for layer in self._workspace_layers:
            layer_id = layer.id
            layer_dir = os.path.join(self._staging_dir, f"layer_{layer_id}")
            os.makedirs(layer_dir, exist_ok=True)
            self._partition_staging[layer_id] = layer_dir

            # Clean stale symlinks
            for entry in os.scandir(layer_dir):
                if entry.is_symlink():
                    os.unlink(entry.path)

            # Create symlinks (first-wins within layer)
            staged: Dict[str, str] = {}
            for filepath in layer_files.get(layer_id, []):
                basename = os.path.basename(filepath)
                link_path = os.path.join(layer_dir, basename)
                if basename in staged:
                    continue
                if os.path.lexists(link_path):
                    os.unlink(link_path)
                try:
                    os.symlink(filepath, link_path)
                    staged[basename] = filepath
                except OSError as exc:
                    logger.warning(
                        "Layer %s: symlink failed for %s: %s",
                        layer_id,
                        filepath,
                        exc,
                    )

            logger.info(
                "Layer '%s' staging: %d own files in %s",
                layer_id,
                len(staged),
                layer_dir,
            )

        # Inject dependency files from depends_on layers.
        # Own files (already symlinked above) take precedence.
        self._layer_by_id = {l.id: l for l in self._workspace_layers}
        if has_deps and deps_valid:
            for layer in self._workspace_layers:
                if not layer.depends_on:
                    continue
                layer_dir = self._partition_staging[layer.id]
                injected_count = 0
                for dep_id in layer.depends_on:
                    if dep_id not in self._layer_by_id:
                        continue  # Already warned in _validate_layer_deps
                    for filepath in layer_files.get(dep_id, []):
                        basename = os.path.basename(filepath)
                        link_path = os.path.join(layer_dir, basename)
                        if os.path.lexists(link_path):
                            continue  # Own files or earlier dep takes precedence
                        try:
                            os.symlink(filepath, link_path)
                            injected_count += 1
                        except OSError as exc:
                            logger.warning(
                                "Layer %s: dep injection symlink failed "
                                "for %s (from %s): %s",
                                layer.id,
                                basename,
                                dep_id,
                                exc,
                            )
                if injected_count:
                    logger.info(
                        "Layer '%s' dependency injection: %d files from %s",
                        layer.id,
                        injected_count,
                        layer.depends_on,
                    )

        # Stage stdlib modules as shared symlinks in all layer dirs.
        # Layer's own files take precedence (symlink already exists → skip).
        std_dir = self._get_std_include_dir()
        stdlib_staged = 0
        if std_dir and os.path.isdir(std_dir):
            for fn in os.listdir(std_dir):
                if not fn.endswith(".ivy"):
                    continue
                src = os.path.join(std_dir, fn)
                for layer in self._workspace_layers:
                    link_path = os.path.join(self._partition_staging[layer.id], fn)
                    if os.path.lexists(link_path):
                        continue  # Layer's own file takes precedence
                    try:
                        os.symlink(src, link_path)
                        stdlib_staged += 1
                    except OSError:
                        pass
            if stdlib_staged:
                logger.info(
                    "Stdlib staging: %d symlinks across %d layers (from %s)",
                    stdlib_staged,
                    len(self._workspace_layers),
                    std_dir,
                )

        # Map files to their layer for resolve_partitioned
        for filepath, layer_id in self._file_to_layer.items():
            self._file_to_partition[filepath] = layer_id

        logger.info(
            "Layered staging active: %d layers, %d files mapped to partitions",
            len(self._partition_staging),
            len(self._file_to_partition),
        )

    def staging_health(self) -> Dict[str, Any]:
        """Return a summary of staging directory health.

        Returns:
            Dict with keys: total_staged, collisions, symlink_failures,
            layers_active, layer_count, files_mapped_to_layers.
        """
        result: Dict[str, Any] = {
            "total_staged": len(self._staged_files),
            "collisions": len(self._collision_map),
            "collision_basenames": sorted(self._collision_map.keys())[:20],
            "layers_active": bool(self._partition_staging),
            "layer_count": len(self._partition_staging),
            "files_mapped_to_layers": len(self._file_to_partition),
        }
        # Check for broken symlinks in staging dir
        symlink_failures = 0
        if self._staging_dir and os.path.isdir(self._staging_dir):
            for entry in os.scandir(self._staging_dir):
                if entry.is_symlink() and not os.path.exists(entry.path):
                    symlink_failures += 1
        result["symlink_failures"] = symlink_failures
        return result

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

            ivy_dir = os.path.dirname(os.path.realpath(ivy_mod.__file__))
            inc_base = os.path.join(ivy_dir, "include")
            if not os.path.isdir(inc_base):
                return None
            # Prefer 1.7 (matching #lang ivy1.7), fall back to highest
            preferred = os.path.join(inc_base, "1.7")
            if os.path.isdir(preferred):
                return preferred
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
