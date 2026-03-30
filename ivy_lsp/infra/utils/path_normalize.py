"""Path normalization for consistent path handling across MCP/LSP tools."""

import os
from typing import Optional


def normalize_ivy_path(
    path: str,
    workspace_root: str,
    *,
    prefix: str = "protocol-testing",
) -> str:
    """Normalize user-supplied path against workspace root.

    Tries: direct, with prefix, without prefix. Returns first that exists.
    """
    if os.path.isabs(path):
        return path
    direct = os.path.join(workspace_root, path)
    if os.path.exists(direct):
        return direct
    with_prefix = os.path.join(workspace_root, prefix, path)
    if os.path.exists(with_prefix):
        return with_prefix
    for p in [prefix + "/", prefix + os.sep]:
        if path.startswith(p):
            without = os.path.join(workspace_root, path[len(p) :])
            if os.path.exists(without):
                return without
    return direct


def strip_prefix(path: str, prefix: str = "protocol-testing") -> str:
    """Remove protocol-testing/ prefix if present."""
    for p in [prefix + "/", prefix + os.sep]:
        if path.startswith(p):
            return path[len(p) :]
    return path


def ensure_prefix(path: str, prefix: str = "protocol-testing") -> str:
    """Add protocol-testing/ prefix if not present."""
    for p in [prefix + "/", prefix + os.sep]:
        if path.startswith(p):
            return path
    return os.path.join(prefix, path)


def normalize_file_filter(
    file_filter: str,
    reference_paths: list[str],
) -> Optional[str]:
    """Match file_filter against known paths via exact, basename, or suffix."""
    if file_filter in reference_paths:
        return file_filter
    basename = os.path.basename(file_filter)
    for ref in reference_paths:
        if os.path.basename(ref) == basename:
            return ref
    for ref in reference_paths:
        if ref.endswith("/" + file_filter) or ref.endswith(os.sep + file_filter):
            return ref
    return None


def relativize_path(abs_path: str, workspace_root: str) -> str:
    """Strip workspace root prefix to produce a relative path."""
    if not abs_path or not workspace_root:
        return abs_path
    if abs_path.startswith(workspace_root):
        rel = abs_path[len(workspace_root) :]
        return rel.lstrip(os.sep)
    return abs_path


class PathResolver:
    """Single source of truth for path canonicalization within a protocol workspace.

    All internal ivy-lsp components should use this instead of bare
    os.path.abspath() calls to ensure consistent path representation.
    """

    def __init__(self, protocol_dir: str) -> None:
        """Initialize with the protocol directory as the resolution base."""
        self._protocol_dir = os.path.realpath(os.path.abspath(protocol_dir))

    @property
    def protocol_dir(self) -> str:
        """Return the canonical absolute protocol directory."""
        return self._protocol_dir

    def to_absolute(self, rel_path: str) -> str:
        """Convert offline-index relative path to canonical absolute."""
        if os.path.isabs(rel_path):
            return os.path.realpath(rel_path)
        return os.path.realpath(os.path.join(self._protocol_dir, rel_path))

    def to_relative(self, abs_path: str) -> str:
        """Convert absolute path to protocol-relative."""
        return os.path.relpath(abs_path, self._protocol_dir)

    def canonicalize(self, path: str) -> str:
        """Canonicalize any path (abs or rel) to its resolved absolute form."""
        return os.path.realpath(path) if os.path.isabs(path) else self.to_absolute(path)
