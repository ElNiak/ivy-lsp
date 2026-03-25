"""Abstract base class for staging strategies.

Staging strategies handle the creation and management of temporary
directories with symlinks that satisfy the Ivy parser's flat-directory
requirement. Different strategies serve different contexts:

- FlatStagingStrategy: One directory, all symlinks (for external tools)
- LayeredStagingStrategy: Per-layer directories (for multi-protocol)
- VirtualStagingStrategy: No filesystem (for LSP-only resolution)
- ContentAddressedStagingStrategy: Hash-based reusable dirs
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class StagingResult:
    """Result of a staging preparation operation.

    Frozen to prevent mutation after creation. Use metadata for
    strategy-specific data (layer mappings, partition IDs).
    """

    staging_dir: Optional[str]
    staged_files: Dict[str, str]  # basename -> original absolute path
    collision_map: Dict[str, List[str]]  # basename -> all colliding paths
    # C4: extensibility for LayeredStagingStrategy (Plan 4) —
    # carries file_to_layer, partition IDs, etc. without bloating base type
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> StagingResult:
        """Create an empty result (no staging performed)."""
        return cls(staging_dir=None, staged_files={}, collision_map={})


class StagingStrategy(ABC):
    """Abstract interface for staging strategies."""

    @abstractmethod
    def prepare(
        self,
        source_files: List[str],
        workspace_root: str,
        workspace_layers: Optional[List[Any]] = None,
    ) -> StagingResult:
        """Create the staging directory with symlinks.

        Args:
            source_files: All .ivy file paths to stage.
            workspace_root: Root directory of the workspace.
            workspace_layers: Optional layer configuration.

        Returns:
            StagingResult with staging directory path and file mappings.
        """

    @abstractmethod
    def resolve(self, include_name: str, from_file: str) -> Optional[str]:
        """Resolve an include name using the staging directory.

        Args:
            include_name: Bare include name (without .ivy extension).
            from_file: Absolute path of the file containing the include.

        Returns:
            Absolute path to the resolved file, or None.
        """

    @abstractmethod
    def cleanup(self) -> None:
        """Remove staging directory and clear state."""

    @abstractmethod
    def get_dir_for_file(self, filepath: str) -> Optional[str]:
        """Return the staging directory path relevant to a file.

        For flat staging, returns the single staging dir.
        For layered staging, returns the partition dir for the file's layer.
        """

    @property
    @abstractmethod
    def is_active(self) -> bool:
        """Whether staging has been prepared and is usable."""
