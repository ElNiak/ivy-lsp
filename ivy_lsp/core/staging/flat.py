"""Flat staging strategy -- one directory with one symlink per .ivy file.

Extracted from IncludeResolver.create_staging_directory(). Creates a
single temp directory where each basename maps to exactly one file.
First sorted path wins for basename collisions.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

from ivy_lsp.core.staging.strategy import StagingResult, StagingStrategy

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_SECS = 3600


class FlatStagingStrategy(StagingStrategy):
    """Create a flat temp directory with one symlink per .ivy basename."""

    def __init__(self) -> None:  # noqa: D107
        self._staging_dir: Optional[str] = None
        self._staged_files: Dict[str, str] = {}
        self._collision_map: Dict[str, List[str]] = {}

    def prepare(
        self,
        source_files: List[str],
        workspace_root: str,
        workspace_layers: Optional[List[Any]] = None,
    ) -> StagingResult:
        """Create flat staging directory with symlinks."""
        self._cleanup_stale_dirs()

        staging = tempfile.mkdtemp(prefix="ivy-lsp-stage-")
        atexit.register(lambda d=staging: shutil.rmtree(d, ignore_errors=True))
        self._staging_dir = staging
        self._staged_files.clear()
        self._collision_map.clear()

        # Build collision map (basename -> all source paths)
        basename_to_paths: Dict[str, List[str]] = {}
        for filepath in source_files:
            basename = os.path.basename(filepath)
            basename_to_paths.setdefault(basename, []).append(filepath)

        for basename, paths in basename_to_paths.items():
            if len(paths) > 1:
                self._collision_map[basename] = list(paths)

        # Create symlinks (sorted order, first wins)
        for filepath in sorted(source_files):
            basename = os.path.basename(filepath)
            link_path = os.path.join(staging, basename)
            if os.path.lexists(link_path):
                continue
            try:
                os.symlink(filepath, link_path)
                self._staged_files[basename] = filepath
            except OSError as exc:
                logger.warning("Failed to create symlink for %s: %s", filepath, exc)

        # C2 fix: detect total staging failure
        if source_files and not self._staged_files:
            logger.error(
                "Staging failed: %d source files but no symlinks created in %s. "
                "Include resolution will fall back to workspace root only.",
                len(source_files),
                staging,
            )

        return StagingResult(
            staging_dir=staging,
            staged_files=dict(self._staged_files),
            collision_map=dict(self._collision_map),
        )

    def resolve(self, include_name: str, from_file: str) -> Optional[str]:
        """Resolve via the flat staging directory."""
        if not self._staging_dir:
            return None
        fname = include_name + ".ivy"
        candidate = os.path.join(self._staging_dir, fname)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)
        return None

    def cleanup(self) -> None:
        """Remove staging directory. H4 fix: restore onerror logging."""
        if self._staging_dir and os.path.isdir(self._staging_dir):

            def _on_error(func, path, exc_info):
                logger.warning(
                    "Staging cleanup error: %s on %s: %s",
                    func.__name__,
                    path,
                    exc_info[1],
                )

            shutil.rmtree(self._staging_dir, onerror=_on_error)
        self._staging_dir = None
        self._staged_files.clear()
        self._collision_map.clear()

    def get_dir_for_file(self, filepath: str) -> Optional[str]:
        """Return the single staging directory."""
        return self._staging_dir

    @property
    def is_active(self) -> bool:
        """Whether staging has been prepared and is usable."""
        return self._staging_dir is not None and os.path.isdir(self._staging_dir)

    @property
    def collision_map(self) -> Dict[str, List[str]]:
        """Basename collision map from last prepare()."""
        return dict(self._collision_map)

    @property
    def staged_files(self) -> Dict[str, str]:
        """Basename -> original path from last prepare()."""
        return dict(self._staged_files)

    def _cleanup_stale_dirs(self) -> None:
        """Remove staging directories older than threshold."""
        tmpdir = tempfile.gettempdir()
        now = time.time()
        for entry in os.scandir(tmpdir):
            if entry.name.startswith("ivy-lsp-stage-") and entry.is_dir(
                follow_symlinks=False
            ):
                try:
                    if now - entry.stat().st_mtime > _STALE_THRESHOLD_SECS:
                        shutil.rmtree(entry.path, ignore_errors=True)
                except OSError:
                    pass
