# Foundation: Mirror + MirrorRegistry + StagingStrategy Extraction

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `Mirror` as a first-class NCT entity (evolving `TestScope`) and extract the staging system from `IncludeResolver` into a `StagingStrategy` interface — the foundation for the full indexing/workspace refactoring.

**Architecture:** `Mirror` wraps `TestScope` with protocol binding, stable identity (`MirrorId`), and role semantics. `MirrorRegistry` provides reverse-index lookups (file → mirrors). `StagingStrategy` is an ABC extracted from `IncludeResolver`, with `FlatStagingStrategy` as the first concrete implementation. Both are additive — existing code continues to work via backward-compat bridges.

**Tech Stack:** Python 3.10+, dataclasses, pytest, threading

**Plan series:** This is Plan 1 of 3. Plan 2 covers IndexSnapshot/SnapshotBuilder. Plan 3 covers ScopeResolver/Workspace/MCP.

---

## Review-Driven Design Decisions

These decisions were made after 4-perspective review (type design, silent failures, test coverage, architecture):

| ID | Decision | Rationale |
|----|----------|-----------|
| C1 | `MirrorRegistry.register()` cleans stale reverse-index entries on re-registration | Prevents phantom mirror references after incremental re-index |
| C2 | `FlatStagingStrategy.prepare()` logs ERROR when all symlinks fail | Prevents silent degradation of include resolution |
| C4 | `StagingResult` has `metadata: Dict[str, Any]` field | Extensibility for LayeredStagingStrategy (Plan 4) without bloating base type |
| C5 | `IncludeResolver.resolve()` delegates step 2 to `strategy.resolve()` | Enables VirtualStagingStrategy in Plan 4 |
| H1 | All MirrorRegistry read methods acquire `_lock` | Thread safety for concurrent index + query |
| H2 | `Mirror.protocol` is a property derived from `id.protocol` | Eliminates redundancy and divergence risk |
| H3 | `MirrorRegistry.to_snapshot_dict()` added | Plan 2 needs immutable mirror dict for `IndexSnapshot` |
| H4 | `FlatStagingStrategy.cleanup()` uses `onerror` callback | Restores existing logging behavior lost in extraction |
| H5 | `StagingResult` is `frozen=True` | Prevent mutation after creation |
| H6 | `TestScope.from_mirror()` NOT added in Plan 1 | Avoids circular import. Bridge is one-directional: `Mirror` knows `TestScope`, not vice versa |
| H7 | `protocol="unknown"` accepted as intermediate state | MirrorIds will change when Plan 3 wires `NctWorkspace`. No persistent storage of MirrorIds before then |
| M1 | `Mirror.from_test_scope()` uses defensive role conversion | Unknown role strings fall back to `UNKNOWN` instead of crashing |
| M5 | `MirrorId.from_test_file()` uses `.removesuffix(".ivy")` | Prevents stripping `.ivy` from middle of filename |

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `ivy_lsp/core/analysis/mirror.py` | `MirrorId`, `MirrorRole`, `Mirror`, `MirrorRegistry` |
| `ivy_lsp/core/staging/__init__.py` | Public API for staging package |
| `ivy_lsp/core/staging/strategy.py` | `StagingStrategy` ABC, `StagingResult` dataclass |
| `ivy_lsp/core/staging/flat.py` | `FlatStagingStrategy` — extracted from `IncludeResolver.create_staging_directory()` |
| `tests/test_mirror.py` | Tests for Mirror, MirrorId, MirrorRegistry |
| `tests/test_staging_strategy.py` | Tests for StagingStrategy, FlatStagingStrategy |

### Modified Files
| File | Change |
|------|--------|
| `ivy_lsp/core/indexer/scope_manager.py:258-316` | Create `Mirror` objects alongside `TestScope` in `_compute_test_scopes()` |
| `ivy_lsp/core/indexer/include_resolver.py:467-583` | Delegate `create_staging_directory()` to injected `FlatStagingStrategy`; wire `strategy.resolve()` into `resolve()` |
| `ivy_lsp/core/indexer/workspace_indexer.py` | Add `_mirror_registry` attribute, expose via property |

---

### Task 1: MirrorId and MirrorRole

**Files:**
- Create: `ivy_lsp/core/analysis/mirror.py`
- Test: `tests/test_mirror.py`

- [ ] **Step 1: Write failing tests for MirrorId**

```python
# tests/test_mirror.py
"""Tests for Mirror, MirrorId, and MirrorRegistry."""

import os

import pytest

from ivy_lsp.core.analysis.mirror import MirrorId, MirrorRole


class TestMirrorId:
    def test_create_from_fields(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_str_representation(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert str(mid) == "quic::quic_server_test_stream"

    def test_from_test_file(self):
        mid = MirrorId.from_test_file(
            "/path/to/quic_server_test_stream.ivy", protocol="quic"
        )
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_from_test_file_strips_ivy_extension(self):
        mid = MirrorId.from_test_file("/any/path/foo_test.ivy", protocol="minip")
        assert mid.entry_stem == "foo_test"

    def test_hashable_and_eq(self):
        a = MirrorId(protocol="quic", entry_stem="test_a")
        b = MirrorId(protocol="quic", entry_stem="test_a")
        c = MirrorId(protocol="quic", entry_stem="test_b")
        assert a == b
        assert a != c
        assert {a, b, c} == {a, c}

    def test_from_test_file_no_extension(self):
        mid = MirrorId.from_test_file("/path/to/test_file", protocol="quic")
        assert mid.entry_stem == "test_file"

    def test_frozen(self):
        mid = MirrorId(protocol="quic", entry_stem="test")
        with pytest.raises(AttributeError):
            mid.protocol = "other"


class TestMirrorRole:
    def test_values(self):
        assert MirrorRole.CLIENT.value == "client"
        assert MirrorRole.SERVER.value == "server"
        assert MirrorRole.MIM.value == "mim"
        assert MirrorRole.UNKNOWN.value == "unknown"

    def test_from_string(self):
        assert MirrorRole("client") == MirrorRole.CLIENT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py -x -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ivy_lsp.core.analysis.mirror'`

- [ ] **Step 3: Implement MirrorId and MirrorRole**

```python
# ivy_lsp/core/analysis/mirror.py
"""First-class NCT mirror model.

A mirror represents an endpoint test entry point in the NCT methodology.
It captures the transitive include closure, exported/imported actions,
and tester role for a single test file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Literal, Optional

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

    def __str__(self) -> str:
        return f"{self.protocol}::{self.entry_stem}"

    @classmethod
    def from_test_file(cls, test_file: str, protocol: str) -> MirrorId:
        """Create a MirrorId from a test file path and protocol name."""
        stem = os.path.basename(test_file).removesuffix(".ivy")
        return cls(protocol=protocol, entry_stem=stem)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirrorId tests/test_mirror.py::TestMirrorRole -x -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/analysis/mirror.py tests/test_mirror.py
git commit -m "feat(mirror): add MirrorId and MirrorRole data types"
```

---

### Task 2: Mirror Dataclass

**Files:**
- Modify: `ivy_lsp/core/analysis/mirror.py`
- Test: `tests/test_mirror.py`

- [ ] **Step 1: Write failing tests for Mirror**

Add to `tests/test_mirror.py`:

```python
from ivy_lsp.core.analysis.mirror import Mirror, MirrorId, MirrorRole


class TestMirror:
    @pytest.fixture
    def sample_mirror(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="quic_server_test"),
            entry_file="/ws/quic_tests/quic_server_test.ivy",
            include_closure=frozenset({
                "/ws/quic_tests/quic_server_test.ivy",
                "/ws/quic_stack/quic_types.ivy",
                "/ws/quic_stack/quic_frame.ivy",
            }),
            exported_actions=frozenset({"send_frame", "recv_frame"}),
            imported_actions=frozenset({"accept_connection"}),
            role=MirrorRole.CLIENT,
            protocol_version="rfc9000",
        )

    def test_is_file_in_scope(self, sample_mirror):
        assert sample_mirror.is_file_in_scope("/ws/quic_stack/quic_types.ivy")
        assert not sample_mirror.is_file_in_scope("/ws/other/unrelated.ivy")

    def test_file_count(self, sample_mirror):
        assert sample_mirror.file_count() == 3

    def test_frozen(self, sample_mirror):
        with pytest.raises(AttributeError):
            sample_mirror.protocol = "other"

    def test_to_test_scope(self, sample_mirror):
        scope = sample_mirror.to_test_scope()
        assert scope.test_file == "/ws/quic_tests/quic_server_test.ivy"
        assert scope.include_closure == sample_mirror.include_closure
        assert scope.exported_actions == sample_mirror.exported_actions
        assert scope.imported_actions == sample_mirror.imported_actions
        assert scope.tester_role == "client"

    def test_from_test_scope(self):
        from ivy_lsp.core.analysis.test_scope import TestScope

        scope = TestScope(
            test_file="/ws/test.ivy",
            include_closure=frozenset({"/ws/test.ivy", "/ws/types.ivy"}),
            exported_actions=frozenset({"action_a"}),
            imported_actions=frozenset({"action_b"}),
            tester_role="server",
        )
        mirror = Mirror.from_test_scope(scope, protocol="quic", protocol_version="rfc9000")
        assert mirror.id == MirrorId(protocol="quic", entry_stem="test")
        assert mirror.entry_file == "/ws/test.ivy"
        assert mirror.include_closure == scope.include_closure
        assert mirror.role == MirrorRole.SERVER
        assert mirror.protocol_version == "rfc9000"

    def test_from_test_scope_invalid_role_falls_back(self):
        """M1 fix: unknown tester_role falls back to UNKNOWN, not crash."""
        from ivy_lsp.core.analysis.test_scope import TestScope

        scope = TestScope(
            test_file="/ws/test.ivy",
            include_closure=frozenset({"/ws/test.ivy"}),
            exported_actions=frozenset(),
            imported_actions=frozenset(),
            tester_role="proxy",  # invalid role
        )
        mirror = Mirror.from_test_scope(scope, protocol="quic")
        assert mirror.role == MirrorRole.UNKNOWN

    def test_roundtrip_to_test_scope(self, sample_mirror):
        scope = sample_mirror.to_test_scope()
        roundtripped = Mirror.from_test_scope(
            scope,
            protocol=sample_mirror.protocol,
            protocol_version=sample_mirror.protocol_version,
        )
        assert roundtripped.entry_file == sample_mirror.entry_file
        assert roundtripped.include_closure == sample_mirror.include_closure
        assert roundtripped.role == sample_mirror.role
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirror -x -v`
Expected: FAIL with `ImportError` (Mirror not defined yet)

- [ ] **Step 3: Implement Mirror dataclass**

Add to `ivy_lsp/core/analysis/mirror.py`:

```python
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
                scope.tester_role, scope.test_file,
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirror -x -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/analysis/mirror.py tests/test_mirror.py
git commit -m "feat(mirror): add Mirror dataclass with TestScope bridges"
```

---

### Task 3: MirrorRegistry

**Files:**
- Modify: `ivy_lsp/core/analysis/mirror.py`
- Test: `tests/test_mirror.py`

- [ ] **Step 1: Write failing tests for MirrorRegistry**

Add to `tests/test_mirror.py`:

```python
from ivy_lsp.core.analysis.mirror import MirrorRegistry


class TestMirrorRegistry:
    @pytest.fixture
    def registry(self):
        return MirrorRegistry()

    @pytest.fixture
    def mirror_a(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="server_test"),
            entry_file="/ws/server_test.ivy",
            include_closure=frozenset({
                "/ws/server_test.ivy",
                "/ws/types.ivy",
                "/ws/frame.ivy",
            }),
            exported_actions=frozenset({"send"}),
            imported_actions=frozenset({"recv"}),
            role=MirrorRole.CLIENT,
        )

    @pytest.fixture
    def mirror_b(self):
        return Mirror(
            id=MirrorId(protocol="quic", entry_stem="client_test"),
            entry_file="/ws/client_test.ivy",
            include_closure=frozenset({
                "/ws/client_test.ivy",
                "/ws/types.ivy",
                "/ws/connection.ivy",
            }),
            exported_actions=frozenset({"recv"}),
            imported_actions=frozenset({"send"}),
            role=MirrorRole.SERVER,
        )

    def test_register_and_get(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get(mirror_a.id) is mirror_a

    def test_get_missing_returns_none(self, registry):
        mid = MirrorId(protocol="quic", entry_stem="nonexistent")
        assert registry.get(mid) is None

    def test_get_by_entry_file(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get_by_entry_file("/ws/server_test.ivy") is mirror_a
        assert registry.get_by_entry_file("/ws/nonexistent.ivy") is None

    def test_get_mirrors_for_file_shared(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # types.ivy is in both mirrors
        mirrors = registry.get_mirrors_for_file("/ws/types.ivy")
        ids = {m.id for m in mirrors}
        assert ids == {mirror_a.id, mirror_b.id}

    def test_get_mirrors_for_file_exclusive(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # frame.ivy only in mirror_a
        mirrors = registry.get_mirrors_for_file("/ws/frame.ivy")
        assert len(mirrors) == 1
        assert mirrors[0].id == mirror_a.id

    def test_get_mirrors_for_file_unknown(self, registry, mirror_a):
        registry.register(mirror_a)
        assert registry.get_mirrors_for_file("/ws/unknown.ivy") == []

    def test_get_mirrors_for_protocol(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        quic = registry.get_mirrors_for_protocol("quic")
        assert len(quic) == 2
        assert registry.get_mirrors_for_protocol("minip") == []

    def test_all_mirrors(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        assert len(registry.all_mirrors()) == 2

    def test_invalidate_file(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        # types.ivy is shared — both mirrors affected
        affected = registry.invalidate_file("/ws/types.ivy")
        assert affected == {mirror_a.id, mirror_b.id}

    def test_invalidate_file_exclusive(self, registry, mirror_a, mirror_b):
        registry.register(mirror_a)
        registry.register(mirror_b)
        affected = registry.invalidate_file("/ws/frame.ivy")
        assert affected == {mirror_a.id}

    def test_invalidate_file_unknown(self, registry):
        assert registry.invalidate_file("/ws/unknown.ivy") == set()

    def test_clear(self, registry, mirror_a):
        registry.register(mirror_a)
        registry.clear()
        assert registry.all_mirrors() == []
        assert registry.get(mirror_a.id) is None

    def test_reregister_cleans_stale_reverse_index(self, registry, mirror_a):
        """C1 fix: re-registering with changed closure removes stale entries."""
        registry.register(mirror_a)  # closure includes /ws/frame.ivy
        updated = Mirror(
            id=mirror_a.id, entry_file=mirror_a.entry_file,
            include_closure=frozenset({"/ws/server_test.ivy", "/ws/types.ivy"}),
            exported_actions=mirror_a.exported_actions,
            imported_actions=mirror_a.imported_actions,
            role=mirror_a.role, protocol="quic",
        )
        registry.register(updated)
        # frame.ivy was removed from closure — should not be found
        assert registry.get_mirrors_for_file("/ws/frame.ivy") == []
        # types.ivy still in closure — should still be found
        assert len(registry.get_mirrors_for_file("/ws/types.ivy")) == 1

    def test_to_snapshot_dict(self, registry, mirror_a, mirror_b):
        """H3 fix: snapshot extraction for Plan 2 IndexSnapshot."""
        registry.register(mirror_a)
        registry.register(mirror_b)
        snap = registry.to_snapshot_dict()
        assert len(snap) == 2
        assert mirror_a.id in snap
        # Snapshot is a copy — mutations don't affect registry
        snap.clear()
        assert len(registry.all_mirrors()) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirrorRegistry -x -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement MirrorRegistry**

Add to `ivy_lsp/core/analysis/mirror.py`:

```python
import threading
from collections import defaultdict
from typing import Dict, List, Set


class MirrorRegistry:
    """Thread-safe registry of all known NCT mirrors.

    Provides reverse-index lookups: file → mirrors, protocol → mirrors.
    Replaces the scoping responsibilities split between
    ScopedRequirementModel._test_scopes and WorkspaceContext.protocol_indexes.
    """

    def __init__(self) -> None:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirrorRegistry -x -v`
Expected: PASS (all 12 tests)

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/analysis/mirror.py tests/test_mirror.py
git commit -m "feat(mirror): add MirrorRegistry with reverse-index lookups"
```

---

### Task 4: Wire MirrorRegistry into WorkspaceIndexer

**Files:**
- Modify: `ivy_lsp/core/indexer/workspace_indexer.py`
- Modify: `ivy_lsp/core/indexer/scope_manager.py:258-316`

- [ ] **Step 1: Write failing test for mirror creation in indexer**

```python
# tests/test_mirror.py — add at bottom

class TestMirrorIntegration:
    """Test that _compute_test_scopes populates the mirror registry."""

    def test_compute_test_scopes_populates_registry(self, tmp_path):
        """Verify mirror registry populated after _compute_test_scopes.

        Tests at the ScopeManagerMixin level to avoid needing a full parser.
        We construct a WorkspaceIndexer with a None parser (fast-index only).
        """
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver
        from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer

        # Create a minimal workspace with a test file that has exports
        test_file = tmp_path / "my_test.ivy"
        test_file.write_text(
            "#lang ivy1.7\n\nexport action step\nimport action recv\n"
            "action step\naction recv\n"
        )
        types_file = tmp_path / "types.ivy"
        types_file.write_text("#lang ivy1.7\n\ntype t\n")

        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(
            workspace_root=str(tmp_path),
            parser=None,  # No deep parsing needed
            resolver=resolver,
        )
        indexer.index_workspace()

        # The mirror registry should have been populated
        registry = indexer.mirror_registry
        assert registry is not None
        mirrors = registry.all_mirrors()
        # At least one mirror should exist (for the test file with exports)
        assert len(mirrors) >= 1
        # Protocol defaults to "unknown" (resolved by NctWorkspace in Plan 3)
        assert all(m.protocol == "unknown" for m in mirrors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py::TestMirrorIntegration -x -v`
Expected: FAIL with `AttributeError: 'WorkspaceIndexer' object has no attribute 'mirror_registry'`

- [ ] **Step 3: Add mirror_registry to WorkspaceIndexer**

In `ivy_lsp/core/indexer/workspace_indexer.py`, add import and attribute:

```python
# Add import at top of file (after existing imports)
from ivy_lsp.core.analysis.mirror import MirrorRegistry
```

In the `WorkspaceIndexer.__init__()` method (after line 158: `self._mirror_scope_cache`), add:
```python
self._mirror_registry = MirrorRegistry()
```

Add property (after the existing `stats` property):
```python
@property
def mirror_registry(self) -> MirrorRegistry:
    """Registry of NCT mirrors computed from test scopes."""
    return self._mirror_registry
```

- [ ] **Step 4: Create Mirrors in _compute_test_scopes**

In `ivy_lsp/core/indexer/scope_manager.py`, after the existing `TestScope` registration (line 291), add mirror creation.

Note: `protocol` defaults to `"unknown"` here because the indexer doesn't have workspace context. This will be resolved in Plan 3 when `NctWorkspace` passes protocol info during construction.

```python
# After: self._requirement_graph.register_test_scope(scope)
# Add:
mirror = Mirror.from_test_scope(scope, protocol="unknown")
self._mirror_registry.register(mirror)
```

Also add the import at the top of `scope_manager.py`:
```python
from ivy_lsp.core.analysis.mirror import Mirror
```

And clear the mirror registry at the start of `_compute_test_scopes` (before the `with self._exports_lock:` line):
```python
self._mirror_registry.clear()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_mirror.py -x -v`
Expected: PASS (all tests)

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/ -x --timeout=60 2>&1 | tail -20`
Expected: No new failures (existing failures may remain)

- [ ] **Step 7: Commit**

```bash
git add ivy_lsp/core/indexer/workspace_indexer.py ivy_lsp/core/indexer/scope_manager.py tests/test_mirror.py
git commit -m "feat(mirror): wire MirrorRegistry into WorkspaceIndexer"
```

---

### Task 5: StagingStrategy ABC and StagingResult

**Files:**
- Create: `ivy_lsp/core/staging/__init__.py`
- Create: `ivy_lsp/core/staging/strategy.py`
- Test: `tests/test_staging_strategy.py`

- [ ] **Step 1: Write failing tests for StagingStrategy interface**

```python
# tests/test_staging_strategy.py
"""Tests for StagingStrategy ABC and StagingResult."""

import pytest

from ivy_lsp.core.staging.strategy import StagingResult, StagingStrategy


class TestStagingResult:
    def test_create(self):
        result = StagingResult(
            staging_dir="/tmp/staging",
            staged_files={"types.ivy": "/ws/types.ivy"},
            collision_map={"frame.ivy": ["/ws/a/frame.ivy", "/ws/b/frame.ivy"]},
        )
        assert result.staging_dir == "/tmp/staging"
        assert result.staged_files["types.ivy"] == "/ws/types.ivy"
        assert len(result.collision_map) == 1

    def test_empty(self):
        result = StagingResult.empty()
        assert result.staging_dir is None
        assert result.staged_files == {}
        assert result.collision_map == {}


class TestStagingStrategyABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            StagingStrategy()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py -x -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement StagingStrategy ABC**

```python
# ivy_lsp/core/staging/__init__.py
"""Staging strategy package for Ivy include resolution."""

from ivy_lsp.core.staging.strategy import StagingResult, StagingStrategy

__all__ = ["StagingStrategy", "StagingResult"]
```

```python
# ivy_lsp/core/staging/strategy.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py -x -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/staging/__init__.py ivy_lsp/core/staging/strategy.py tests/test_staging_strategy.py
git commit -m "feat(staging): add StagingStrategy ABC and StagingResult"
```

---

### Task 6: FlatStagingStrategy

**Files:**
- Create: `ivy_lsp/core/staging/flat.py`
- Test: `tests/test_staging_strategy.py`

- [ ] **Step 1: Write failing tests for FlatStagingStrategy**

Add to `tests/test_staging_strategy.py`:

```python
import os

from ivy_lsp.core.staging.flat import FlatStagingStrategy


class TestFlatStagingStrategy:
    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a workspace with .ivy files."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype t\n")
        (tmp_path / "frame.ivy").write_text("#lang ivy1.7\ntype frame\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "conn.ivy").write_text("#lang ivy1.7\ntype conn\n")
        return tmp_path

    @pytest.fixture
    def workspace_with_collisions(self, tmp_path):
        """Workspace where two dirs have files with the same basename."""
        (tmp_path / "proto_a").mkdir()
        (tmp_path / "proto_a" / "types.ivy").write_text("# proto_a types\n")
        (tmp_path / "proto_b").mkdir()
        (tmp_path / "proto_b" / "types.ivy").write_text("# proto_b types\n")
        (tmp_path / "proto_a" / "unique_a.ivy").write_text("# unique_a\n")
        return tmp_path

    def test_prepare_creates_staging_dir(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
            str(workspace / "sub" / "conn.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        assert result.staging_dir is not None
        assert os.path.isdir(result.staging_dir)
        strategy.cleanup()

    def test_prepare_creates_symlinks(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        # Each file should have a symlink in the staging dir
        assert os.path.islink(os.path.join(result.staging_dir, "types.ivy"))
        assert os.path.islink(os.path.join(result.staging_dir, "frame.ivy"))
        assert result.staged_files["types.ivy"] == str(workspace / "types.ivy")
        strategy.cleanup()

    def test_prepare_detects_collisions(self, workspace_with_collisions):
        ws = workspace_with_collisions
        strategy = FlatStagingStrategy()
        source_files = [
            str(ws / "proto_a" / "types.ivy"),
            str(ws / "proto_b" / "types.ivy"),
            str(ws / "proto_a" / "unique_a.ivy"),
        ]
        result = strategy.prepare(source_files, str(ws))
        assert "types.ivy" in result.collision_map
        assert len(result.collision_map["types.ivy"]) == 2
        # First sorted path wins
        assert result.staged_files["types.ivy"] == str(ws / "proto_a" / "types.ivy")
        strategy.cleanup()

    def test_resolve_finds_staged_file(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [str(workspace / "types.ivy")]
        strategy.prepare(source_files, str(workspace))
        resolved = strategy.resolve("types", str(workspace / "frame.ivy"))
        assert resolved is not None
        assert resolved.endswith("types.ivy")
        strategy.cleanup()

    def test_resolve_returns_none_for_unknown(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [str(workspace / "types.ivy")]
        strategy.prepare(source_files, str(workspace))
        assert strategy.resolve("nonexistent", str(workspace / "frame.ivy")) is None
        strategy.cleanup()

    def test_is_active(self, workspace):
        strategy = FlatStagingStrategy()
        assert not strategy.is_active
        strategy.prepare([str(workspace / "types.ivy")], str(workspace))
        assert strategy.is_active
        strategy.cleanup()
        assert not strategy.is_active

    def test_cleanup_removes_dir(self, workspace):
        strategy = FlatStagingStrategy()
        result = strategy.prepare([str(workspace / "types.ivy")], str(workspace))
        staging_dir = result.staging_dir
        strategy.cleanup()
        assert not os.path.exists(staging_dir)

    def test_prepare_empty_file_list(self, workspace):
        strategy = FlatStagingStrategy()
        result = strategy.prepare([], str(workspace))
        assert result.staging_dir is not None
        assert result.staged_files == {}
        assert result.collision_map == {}
        strategy.cleanup()

    def test_prepare_no_collisions(self, workspace):
        strategy = FlatStagingStrategy()
        source_files = [
            str(workspace / "types.ivy"),
            str(workspace / "frame.ivy"),
        ]
        result = strategy.prepare(source_files, str(workspace))
        assert result.collision_map == {}
        strategy.cleanup()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py::TestFlatStagingStrategy -x -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement FlatStagingStrategy**

```python
# ivy_lsp/core/staging/flat.py
"""Flat staging strategy — one directory with one symlink per .ivy file.

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

    def __init__(self) -> None:
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
                len(source_files), staging,
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
                    func.__name__, path, exc_info[1],
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py -x -v`
Expected: PASS (all 11 tests)

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/staging/flat.py tests/test_staging_strategy.py
git commit -m "feat(staging): add FlatStagingStrategy extracted from IncludeResolver"
```

---

### Task 7: Inject FlatStagingStrategy into IncludeResolver

**Files:**
- Modify: `ivy_lsp/core/indexer/include_resolver.py`

- [ ] **Step 1: Write failing test for strategy injection**

```python
# tests/test_staging_strategy.py — add at bottom

class TestIncludeResolverIntegration:
    """Test that IncludeResolver delegates to FlatStagingStrategy."""

    def test_create_staging_populates_strategy(self, tmp_path):
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype t\n")
        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()

        # The resolver should have a staging strategy
        assert hasattr(resolver, "_staging_strategy")
        assert resolver._staging_strategy is not None
        assert resolver._staging_strategy.is_active
        resolver.cleanup_staging()

    def test_cleanup_staging_when_no_strategy(self, tmp_path):
        """Cleanup should not crash when create_staging was never called."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        resolver = IncludeResolver(str(tmp_path))
        resolver.cleanup_staging()  # Should not raise

    def test_delegation_collision_map_matches(self, tmp_path):
        """Behavioral equivalence: collision_map populated correctly."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        (tmp_path / "proto_a").mkdir()
        (tmp_path / "proto_a" / "types.ivy").write_text("# a\n")
        (tmp_path / "proto_b").mkdir()
        (tmp_path / "proto_b" / "types.ivy").write_text("# b\n")
        (tmp_path / "proto_a" / "unique.ivy").write_text("# u\n")

        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()

        assert "types.ivy" in resolver._collision_map
        assert len(resolver._collision_map["types.ivy"]) == 2
        assert "unique.ivy" not in resolver._collision_map
        resolver.cleanup_staging()

    def test_resolve_still_works_after_injection(self, tmp_path):
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype t\n")
        (tmp_path / "main.ivy").write_text("#lang ivy1.7\ninclude types\n")
        resolver = IncludeResolver(str(tmp_path))
        resolver.create_staging_directory()

        resolved = resolver.resolve("types", str(tmp_path / "main.ivy"))
        assert resolved is not None
        assert resolved.endswith("types.ivy")
        resolver.cleanup_staging()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py::TestIncludeResolverIntegration -x -v`
Expected: FAIL with `AttributeError: '_staging_strategy'`

- [ ] **Step 3: Inject strategy into IncludeResolver**

In `ivy_lsp/core/indexer/include_resolver.py`:

Add import at top:
```python
from ivy_lsp.core.staging.flat import FlatStagingStrategy
```

In `__init__`, add:
```python
self._staging_strategy: Optional[FlatStagingStrategy] = None
```

In `create_staging_directory`, at the start (after stale cleanup but before creating the temp dir), create and delegate:
```python
# Delegate to FlatStagingStrategy
self._staging_strategy = FlatStagingStrategy()
source_files = self._find_source_files()
result = self._staging_strategy.prepare(source_files, self._workspace_root)
self._staging_dir = result.staging_dir
self._staged_files = dict(result.staged_files)
self._collision_map = dict(result.collision_map)
# Log collision info (preserve existing logging)
if result.collision_map and self._file_to_layer:
    for basename, paths in result.collision_map.items():
        layers_involved = {
            self._file_to_layer.get(os.path.realpath(p), "unknown")
            for p in paths
        }
        if len(layers_involved) <= 1:
            logger.warning(
                "Intra-layer collision: %s has %d variants in layer '%s'",
                basename, len(paths), next(iter(layers_involved)),
            )
return self._staging_dir
```

Replace the body of `create_staging_directory` (lines 480-583) with this delegation. Keep the method signature and docstring. The collision logging stays in `IncludeResolver` (not in the strategy) because it needs `_file_to_layer` context that the strategy doesn't have:

```python
def create_staging_directory(self) -> str:
    """Create a flat temp directory with one symlink per .ivy file."""
    self._staging_strategy = FlatStagingStrategy()
    source_files = self._find_source_files()
    result = self._staging_strategy.prepare(source_files, self._workspace_root)
    self._staging_dir = result.staging_dir
    self._staged_files = dict(result.staged_files)
    self._collision_map = dict(result.collision_map)

    # Collision classification logging (needs _file_to_layer context)
    for basename, paths in result.collision_map.items():
        if self._file_to_layer:
            layers_involved = {
                self._file_to_layer.get(os.path.realpath(p), "unknown")
                for p in paths
            }
            if len(layers_involved) <= 1:
                logger.warning(
                    "Intra-layer collision: %s has %d variants in layer '%s': %s",
                    basename, len(paths), next(iter(layers_involved)),
                    [os.path.relpath(p, self._workspace_root) for p in paths],
                )
            else:
                logger.debug(
                    "Cross-layer collision (expected): %s spans layers %s",
                    basename, sorted(layers_involved),
                )
        else:
            logger.warning(
                "Basename collision: %s has %d variants: %s",
                basename, len(paths),
                [os.path.relpath(p, self._workspace_root) for p in paths],
            )

    logger.info(
        "Staged %d files (%d collisions, %d unique basenames affected)",
        len(self._staged_files), sum(len(v) for v in result.collision_map.values()),
        len(result.collision_map),
    )
    return self._staging_dir
```

In `cleanup_staging`, also cleanup the strategy:
```python
if self._staging_strategy:
    self._staging_strategy.cleanup()
    self._staging_strategy = None
```

- [ ] **Step 4: C5 fix — Wire strategy.resolve() into IncludeResolver.resolve()**

In `IncludeResolver.resolve()` (lines 233-374), the staging directory lookup (step 2, after "same directory" check) currently reads `self._staging_dir` directly. Add a strategy delegation path so that `VirtualStagingStrategy` (Plan 4) can work:

In the `resolve()` method, after the "same directory" check (step 1) and before the existing partition/staging lookup (step 2), add:

```python
# 2a. Delegate to strategy if available (enables VirtualStagingStrategy in Plan 4)
if self._staging_strategy is not None:
    result = self._staging_strategy.resolve(include_name, from_file_real)
    if result is not None:
        return result
```

This runs before the existing partition/layered staging checks. For `FlatStagingStrategy`, it returns the same result as the existing flat staging path. For future strategies, it enables custom resolution.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_staging_strategy.py -x -v`
Expected: PASS

- [ ] **Step 6: Run full test suite to verify no regressions**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/ -x --timeout=60 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 7: Commit**

```bash
git add ivy_lsp/core/indexer/include_resolver.py tests/test_staging_strategy.py
git commit -m "refactor(staging): inject FlatStagingStrategy into IncludeResolver

Wire strategy.resolve() into IncludeResolver.resolve() step 2
to enable VirtualStagingStrategy in Plan 4."
```

---

### Task 8: Run Full Suite and Final Cleanup

- [ ] **Step 1: Run the complete test suite**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/ -v --timeout=60 2>&1 | tail -40`
Expected: All previously passing tests still pass. New tests pass.

- [ ] **Step 2: Verify imports are clean**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -c "from ivy_lsp.core.analysis.mirror import Mirror, MirrorId, MirrorRole, MirrorRegistry; print('Mirror imports OK')" && python -c "from ivy_lsp.core.staging import StagingStrategy, StagingResult; print('Staging imports OK')" && python -c "from ivy_lsp.core.staging.flat import FlatStagingStrategy; print('FlatStaging imports OK')"`
Expected: All three print "OK"

- [ ] **Step 3: Commit final state**

```bash
git add -A
git status
# Only commit if there are unstaged changes from minor fixups
git commit -m "chore: foundation phase complete — Mirror + StagingStrategy"
```
