"""Active workspace state tracking for the Ivy LSP server.

Tracks which protocol workspace (group of layers) is currently "active"
for the session. This drives scoped symbol resolution, diagnostic filtering,
and coverage tools so they only operate over the relevant subset of ``.ivy``
files instead of the entire repository.

Typical usage::

    from ivy_lsp.core.workspace.active_workspace import ActiveWorkspace

    # Set the active workspace from a test file
    ws = ActiveWorkspace.from_test_file(
        test_file="/path/to/quic_tests/test_client.ivy",
        file_to_layer=resolver._file_to_layer,
        workspace_groups=ws_config.workspace_groups,
        workspace_layers=ws_config.workspace_layers,
    )

    # Check whether a file is in scope
    allowed, reason = ws.is_file_allowed(filepath, resolver._file_to_layer)

    # Persist / restore across sessions
    ws.save("/path/to/.ivy-workspace-state.json")
    ws_restored = ActiveWorkspace.load("/path/to/.ivy-workspace-state.json")
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# State file format version — bump when the schema changes in a breaking way.
_STATE_VERSION = 1

# Sentinel substring that identifies Ivy stdlib files (always allowed).
_STDLIB_SENTINEL = "ivy/include"


@dataclass
class ActiveWorkspace:
    """State snapshot of the currently active protocol workspace.

    Attributes:
        active_group: Name of the active workspace group (e.g. ``"quic"``).
            ``None`` when no group was found (single-layer fallback or cleared).
        active_layers: Set of layer IDs that are in scope (e.g.
            ``{"quic", "quic_tests"}``).
        active_tests: List of specific test file paths to restrict to.
            Empty means *all tests in the active layers* are in scope.
        granularity: Coarseness of the active scope.
            One of ``"protocol"``, ``"role_pair"``, ``"test"``, or ``"none"``.
        set_by: How the workspace was activated.
            One of ``"explicit"``, ``"auto"``, ``"marker"``, or ``"cleared"``.
    """

    active_group: Optional[str]
    active_layers: Set[str]
    active_tests: List[str]
    granularity: str  # "protocol" | "role_pair" | "test" | "none"
    set_by: str  # "explicit" | "auto" | "marker" | "cleared"

    # ------------------------------------------------------------------
    # Factory class methods
    # ------------------------------------------------------------------

    @classmethod
    def cleared(cls) -> ActiveWorkspace:
        """Return a no-restriction state (workspace cleared / not set).

        When the workspace is cleared every file passes ``is_file_allowed``.
        """
        return cls(
            active_group=None,
            active_layers=set(),
            active_tests=[],
            granularity="none",
            set_by="cleared",
        )

    @classmethod
    def load(cls, state_file_path: str) -> ActiveWorkspace:
        """Load workspace state from a JSON state file.

        On any error (file missing, corrupt JSON, unexpected schema) returns
        :meth:`cleared` so the server degrades gracefully.

        Args:
            state_file_path: Path to the ``.ivy-workspace-state.json`` file.

        Returns:
            A populated :class:`ActiveWorkspace`, or ``cleared()`` on failure.
        """
        if not os.path.isfile(state_file_path):
            logger.debug(
                "Workspace state file not found at %s, returning cleared state",
                state_file_path,
            )
            return cls.cleared()

        try:
            with open(state_file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "Failed to load workspace state from %s: %s — returning cleared state",
                state_file_path,
                exc,
            )
            return cls.cleared()

        try:
            return cls(
                active_group=data.get("active_group"),
                active_layers=set(data.get("active_layers", [])),
                active_tests=list(data.get("active_tests", [])),
                granularity=data.get("granularity", "none"),
                set_by=data.get("set_by", "cleared"),
            )
        except Exception as exc:
            logger.warning(
                "Corrupt workspace state schema in %s: %s — returning cleared state",
                state_file_path,
                exc,
            )
            return cls.cleared()

    @classmethod
    def from_test_file(
        cls,
        test_file: str,
        file_to_layer: Dict[str, str],
        workspace_groups: Dict[str, List[str]],
        workspace_layers: Optional[list] = None,
    ) -> ActiveWorkspace:
        """Derive an :class:`ActiveWorkspace` from a specific test file path.

        Resolution order:

        1. Look up *test_file* in *file_to_layer* to find its layer.
        2. Search *workspace_groups* for a group that contains that layer.
        3. If found: activate all layers in that group.
        4. If NOT found: activate just that layer plus its ``depends_on``
           chain (resolved recursively from *workspace_layers* if provided).
        5. If the test file is not in any layer at all: return :meth:`cleared`
           with a warning.

        Args:
            test_file: Absolute path to the Ivy test file.
            file_to_layer: Mapping of absolute file path → layer ID.
            workspace_groups: Mapping of group name → list of layer IDs.
            workspace_layers: Optional list of
                :class:`~ivy_lsp.core.workspace.detection.WorkspaceLayer` objects
                used to resolve ``depends_on`` chains during fallback.

        Returns:
            An :class:`ActiveWorkspace` scoped to the relevant group or layer.
        """
        # Step 1: find which layer this file belongs to
        layer_id = file_to_layer.get(test_file) or file_to_layer.get(
            os.path.realpath(test_file)
        )
        if layer_id is None:
            logger.warning(
                "Test file %s is not tracked in any workspace layer; "
                "returning cleared workspace.",
                test_file,
            )
            return cls.cleared()

        # Step 2: find a workspace group that contains this layer
        matching_group: Optional[str] = None
        for group_name, group_layers in workspace_groups.items():
            if layer_id in group_layers:
                matching_group = group_name
                break

        if matching_group is not None:
            # Step 3: activate the entire group
            group_layers_set = set(workspace_groups[matching_group])
            return cls(
                active_group=matching_group,
                active_layers=group_layers_set,
                active_tests=[test_file],
                granularity="test",
                set_by="auto",
            )

        # Step 4: fallback — activate just this layer + depends_on chain
        logger.warning(
            "Layer '%s' (from test file %s) is not part of any workspace group. "
            "Falling back to single-layer workspace with dependency chain.",
            layer_id,
            test_file,
        )
        fallback_layers = _resolve_layer_with_deps(layer_id, workspace_layers)
        return cls(
            active_group=None,
            active_layers=fallback_layers,
            active_tests=[test_file],
            granularity="test",
            set_by="auto",
        )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def is_set(self) -> bool:
        """Return ``True`` when there is an active workspace restriction.

        A workspace is *set* when both conditions hold:

        - ``granularity`` is not ``"none"``
        - ``active_layers`` is non-empty
        """
        return self.granularity != "none" and len(self.active_layers) > 0

    def is_file_allowed(
        self,
        filepath: str,
        file_to_layer: Dict[str, str],
        scope_views: Optional[object] = None,  # reserved, unused currently
    ) -> Tuple[bool, str]:
        """Decide whether *filepath* is in scope for the active workspace.

        Decision table:

        +-------------------------------+----------+------------------------+
        | Condition                     | Result   | Reason                 |
        +===============================+==========+========================+
        | Workspace not set (cleared)   | True     | ``""``                 |
        +-------------------------------+----------+------------------------+
        | Path contains ``ivy/include`` | True     | ``"stdlib"``           |
        +-------------------------------+----------+------------------------+
        | File's layer ∈ active_layers  | True     | ``"in layer <layer>"`` |
        +-------------------------------+----------+------------------------+
        | File's layer ∉ active_layers  | False    | descriptive message    |
        +-------------------------------+----------+------------------------+
        | File not in file_to_layer     | True     | ``"unlayered"``        |
        +-------------------------------+----------+------------------------+

        Args:
            filepath: Absolute path to the file being checked.
            file_to_layer: Mapping of absolute file path → layer ID (from
                the include resolver).
            scope_views: Reserved for future scope view integration (unused).

        Returns:
            A ``(allowed, reason)`` tuple where *reason* is a human-readable
            string explaining the decision.
        """
        # Not set: everything is allowed
        if not self.is_set():
            return (True, "")

        # Stdlib files (ivy/include) are always allowed regardless of scope
        if _STDLIB_SENTINEL in filepath:
            return (True, "stdlib")

        # Look up file's layer
        layer = file_to_layer.get(filepath) or file_to_layer.get(
            os.path.realpath(filepath)
        )

        if layer is None:
            # File not tracked in any layer — fail-open for unknown files
            return (True, "unlayered")

        if layer in self.active_layers:
            return (True, f"in layer {layer}")

        # Layer is known but not in active workspace
        return (
            False,
            f"layer '{layer}' is not in active workspace '{self.active_group}' "
            f"(active layers: {sorted(self.active_layers)})",
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, state_file_path: str) -> None:
        """Write the current workspace state to a JSON file.

        The state file can be loaded back with :meth:`load`.

        Args:
            state_file_path: Destination path for the state JSON file.

        Raises:
            OSError: If the file cannot be written.
        """
        data = {
            "version": _STATE_VERSION,
            "active_group": self.active_group,
            "active_layers": sorted(self.active_layers),
            "active_tests": self.active_tests,
            "granularity": self.granularity,
            "set_by": self.set_by,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        with open(state_file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        logger.debug("Workspace state saved to %s", state_file_path)


@dataclass(frozen=True)
class ScopeProjection:
    """Frozen query-time filter for workspace layer visibility.

    Unlike :class:`ActiveWorkspace` (which manages mutable session state),
    ``ScopeProjection`` is a lightweight, immutable snapshot used by query
    paths to filter results based on which layers are currently active.

    Attributes:
        active_layers: The set of layer IDs considered "in scope".
            An empty frozenset means *no restriction* (everything visible).
        file_to_layer: Mapping of file path to its owning layer ID.
    """

    active_layers: frozenset
    file_to_layer: dict

    def is_visible(self, filepath: str) -> bool:
        """Return ``True`` if *filepath* should be included in query results.

        Decision rules:

        * If ``active_layers`` is empty, every file is visible (no restriction).
        * If *filepath* is not present in ``file_to_layer``, it is visible
          (unknown files are included by default).
        * Otherwise, the file is visible only when its layer is in
          ``active_layers``.
        """
        if not self.active_layers:
            return True  # no restriction
        layer = self.file_to_layer.get(filepath)
        if layer is None:
            return True  # unknown files are visible by default
        return layer in self.active_layers


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _resolve_layer_with_deps(
    layer_id: str,
    workspace_layers: Optional[list],
) -> Set[str]:
    """Collect *layer_id* and all of its transitive ``depends_on`` layers.

    Performs a BFS/iterative resolution over the ``depends_on`` field of each
    :class:`~ivy_lsp.core.workspace.detection.WorkspaceLayer`.

    Args:
        layer_id: The seed layer to start from.
        workspace_layers: List of WorkspaceLayer objects (may be ``None``).

    Returns:
        A set of layer IDs including *layer_id* and all its dependencies.
    """
    result: Set[str] = {layer_id}

    if not workspace_layers:
        return result

    # Build a quick lookup map
    layer_map: Dict[str, list] = {
        getattr(layer, "id", ""): getattr(layer, "depends_on", [])
        for layer in workspace_layers
    }

    # BFS over depends_on
    queue = list(layer_map.get(layer_id, []))
    visited: Set[str] = {layer_id}

    while queue:
        dep = queue.pop(0)
        if dep in visited:
            continue
        visited.add(dep)
        result.add(dep)
        queue.extend(layer_map.get(dep, []))

    return result
