"""First-class NCT mirror model.

A mirror represents an endpoint test entry point in the NCT methodology.
It captures the transitive include closure, exported/imported actions,
and tester role for a single test file.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set

logger = logging.getLogger(__name__)


class MirrorRole(str, Enum):
    """Tester role in an NCT mirror (accounts for role inversion)."""

    CLIENT = "client"
    SERVER = "server"
    MIM = "mim"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MirrorId:
    """Stable, hashable identity for an NCT mirror.

    Combines protocol + entry point basename for a globally unique key.
    """

    protocol: str
    entry_stem: str

    def __str__(self) -> str:  # noqa: D105
        return f"{self.protocol}::{self.entry_stem}"

    @classmethod
    def from_test_file(cls, test_file: str, protocol: str) -> MirrorId:
        """Create a MirrorId from a test file path and protocol name."""
        stem = os.path.basename(test_file).removesuffix(".ivy")
        return cls(protocol=protocol, entry_stem=stem)


@dataclass(frozen=True)
class Mirror:
    """First-class representation of an NCT endpoint mirror.

    Evolves TestScope with protocol binding, stable identity, and
    role semantics. Use from_test_scope() / to_test_scope() for
    backward compatibility with existing code.

    Note: ``protocol`` is derived from ``id.protocol`` (property, not field)
    to prevent divergence between the two.
    """

    id: MirrorId
    entry_file: str
    include_closure: FrozenSet[str]
    exported_actions: FrozenSet[str]
    imported_actions: FrozenSet[str]
    role: MirrorRole
    protocol_version: Optional[str] = None

    @property
    def protocol(self) -> str:
        """Protocol name, derived from id to prevent redundancy."""
        return self.id.protocol

    def is_file_in_scope(self, filepath: str) -> bool:
        """Check if a file is in this mirror's include closure."""
        return filepath in self.include_closure

    def file_count(self) -> int:
        """Number of files in the include closure."""
        return len(self.include_closure)

    def to_test_scope(self) -> "TestScope":
        """Convert to legacy TestScope for backward compatibility."""
        from ivy_lsp.core.analysis.test_scope import TestScope

        return TestScope(
            test_file=self.entry_file,
            include_closure=self.include_closure,
            exported_actions=self.exported_actions,
            imported_actions=self.imported_actions,
            tester_role=self.role.value,
        )

    @classmethod
    def from_test_scope(
        cls,
        scope: "TestScope",
        protocol: str,
        protocol_version: Optional[str] = None,
    ) -> Mirror:
        """Upgrade a legacy TestScope to a Mirror.

        Defensively converts tester_role — unknown role strings
        fall back to MirrorRole.UNKNOWN instead of crashing.
        """
        try:
            role = MirrorRole(scope.tester_role)
        except ValueError:
            logger.warning(
                "Unknown tester_role '%s' for %s, defaulting to UNKNOWN",
                scope.tester_role,
                scope.test_file,
            )
            role = MirrorRole.UNKNOWN
        return cls(
            id=MirrorId.from_test_file(scope.test_file, protocol),
            entry_file=scope.test_file,
            include_closure=scope.include_closure,
            exported_actions=scope.exported_actions,
            imported_actions=scope.imported_actions,
            role=role,
            protocol_version=protocol_version,
        )


class MirrorRegistry:
    """Thread-safe registry of all known NCT mirrors.

    Provides reverse-index lookups: file -> mirrors, protocol -> mirrors.
    Replaces the scoping responsibilities split between
    ScopedRequirementModel._test_scopes and WorkspaceContext.protocol_indexes.
    """

    def __init__(self) -> None:  # noqa: D107
        self._mirrors: Dict[MirrorId, Mirror] = {}
        self._by_entry_file: Dict[str, Mirror] = {}
        self._file_to_mirrors: Dict[str, Set[MirrorId]] = defaultdict(set)
        self._lock = threading.RLock()

    def register(self, mirror: Mirror) -> None:
        """Register a mirror, updating all indexes.

        If a mirror with the same ID was previously registered, its stale
        reverse-index entries are cleaned before adding the new ones.
        """
        with self._lock:
            old = self._mirrors.get(mirror.id)
            if old is not None:
                # Remove stale reverse-index entries from old closure
                for f in old.include_closure:
                    s = self._file_to_mirrors.get(f)
                    if s is not None:
                        s.discard(mirror.id)
                        if not s:
                            del self._file_to_mirrors[f]
                if old.entry_file in self._by_entry_file:
                    del self._by_entry_file[old.entry_file]
            self._mirrors[mirror.id] = mirror
            self._by_entry_file[mirror.entry_file] = mirror
            for f in mirror.include_closure:
                self._file_to_mirrors[f].add(mirror.id)

    def get(self, mirror_id: MirrorId) -> Optional[Mirror]:
        """Get a mirror by its ID, or None."""
        return self._mirrors.get(mirror_id)

    def get_by_entry_file(self, entry_file: str) -> Optional[Mirror]:
        """Get a mirror by its entry file path, or None."""
        return self._by_entry_file.get(entry_file)

    def get_mirrors_for_file(self, filepath: str) -> List[Mirror]:
        """All mirrors whose include closure contains filepath."""
        with self._lock:
            ids = self._file_to_mirrors.get(filepath, set())
            return [self._mirrors[mid] for mid in ids if mid in self._mirrors]

    def get_mirrors_for_protocol(self, protocol: str) -> List[Mirror]:
        """All mirrors for a given protocol."""
        with self._lock:
            return [m for m in self._mirrors.values() if m.protocol == protocol]

    def all_mirrors(self) -> List[Mirror]:
        """All registered mirrors."""
        with self._lock:
            return list(self._mirrors.values())

    def invalidate_file(self, filepath: str) -> Set[MirrorId]:
        """Return mirror IDs affected by a change to filepath."""
        with self._lock:
            return set(self._file_to_mirrors.get(filepath, set()))

    def to_snapshot_dict(self) -> Dict[MirrorId, Mirror]:
        """Return an immutable copy for IndexSnapshot (Plan 2)."""
        with self._lock:
            return dict(self._mirrors)

    def clear(self) -> None:
        """Remove all mirrors and indexes."""
        with self._lock:
            self._mirrors.clear()
            self._by_entry_file.clear()
            self._file_to_mirrors.clear()
