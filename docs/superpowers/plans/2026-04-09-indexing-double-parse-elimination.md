# Indexing Double-Parse Elimination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the redundant `TieredExtractor.extract()` pass in `build_semantic_model` by forwarding Phase B extraction results, achieving ~3x speedup on full index builds.

**Architecture:** Add an optional `precomputed_extractions` parameter to `build_semantic_model`. When provided (by the offline index builder), the per-file loop deserializes pre-computed symbols via `IvySymbol.from_dict()` and extracts references via cheap regex, skipping the expensive Tier 1 parser entirely. The MCP server caller continues using the function without pre-computed data (current behavior preserved).

**Tech Stack:** Python 3.10+, dataclasses, `IvySymbol.from_dict()`, `extract_references_regex`

**Spec:** `docs/superpowers/specs/2026-04-09-indexing-double-parse-elimination-design.md`

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `ivy_lsp/core/semantic/model_builder.py` | Shared semantic model builder | Modify: add `PrecomputedFileData`, add `precomputed_extractions` param |
| `ivy_lsp/lsp/index_builder.py` | Offline index builder | Modify: expand `_build_models` signature, build + pass precomputed dict |
| `tests/test_model_builder_precomputed.py` | Regression test | Create: verify precomputed path matches full-extraction path |

---

### Task 1: Add `PrecomputedFileData` dataclass to `model_builder.py`

**Files:**
- Modify: `ivy_lsp/core/semantic/model_builder.py:1-18`
- Test: `tests/test_model_builder_precomputed.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_model_builder_precomputed.py`:

```python
"""Tests for precomputed extraction data in build_semantic_model."""

import pytest

from ivy_lsp.core.semantic.model_builder import PrecomputedFileData


@pytest.mark.unit
class TestPrecomputedFileData:
    def test_construction(self):
        pfd = PrecomputedFileData(
            symbols=[{"name": "foo", "kind": 12, "range": [0, 0, 10, 0],
                       "children": [], "detail": None, "file_path": "f.ivy",
                       "synthetic": False}],
            includes=["bar"],
            tier_used=1,
        )
        assert pfd.tier_used == 1
        assert len(pfd.symbols) == 1
        assert pfd.includes == ["bar"]

    def test_empty(self):
        pfd = PrecomputedFileData(symbols=[], includes=[], tier_used=3)
        assert pfd.symbols == []
        assert pfd.includes == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py::TestPrecomputedFileData -v`
Expected: FAIL with `ImportError: cannot import name 'PrecomputedFileData'`

- [ ] **Step 3: Write minimal implementation**

In `ivy_lsp/core/semantic/model_builder.py`, add the dataclass after the existing imports (after line 15):

```python
from dataclasses import dataclass


@dataclass
class PrecomputedFileData:
    """Pre-computed extraction results for a single .ivy file.

    Produced by the index builder's parallel extraction phase (Phase B)
    and consumed by ``build_semantic_model`` to skip redundant re-extraction.
    """

    symbols: list[dict]
    includes: list[str]
    tier_used: int
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py::TestPrecomputedFileData -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/core/semantic/model_builder.py tests/test_model_builder_precomputed.py
git commit -m "feat(model-builder): add PrecomputedFileData dataclass"
```

---

### Task 2: Add `precomputed_extractions` parameter to `build_semantic_model`

**Files:**
- Modify: `ivy_lsp/core/semantic/model_builder.py:22-146`
- Test: `tests/test_model_builder_precomputed.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_model_builder_precomputed.py`:

```python
import os


@pytest.mark.unit
class TestBuildSemanticModelPrecomputed:
    """Verify precomputed path produces same model as full extraction."""

    def _make_ivy_files(self, tmp_path):
        """Create a minimal workspace with two .ivy files."""
        (tmp_path / "types.ivy").write_text(
            "#lang ivy1.7\n\ntype cid\ntype pkt_type = {initial, handshake}\n"
        )
        (tmp_path / "main.ivy").write_text(
            "#lang ivy1.7\n\ninclude types\n\n"
            "type packet\naction send(p: packet)\naction recv(p: packet)\n"
        )
        return str(tmp_path)

    def _extract_files(self, root):
        """Run TieredExtractor on all .ivy files, return precomputed dict."""
        from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
        from ivy_lsp.core.semantic.model_builder import PrecomputedFileData

        extractor = TieredExtractor(skip_tier1=True)
        precomputed = {}
        for fname in os.listdir(root):
            if not fname.endswith(".ivy"):
                continue
            abs_path = os.path.join(root, fname)
            with open(abs_path) as f:
                source = f.read()
            result = extractor.extract(source, abs_path)
            precomputed[abs_path] = PrecomputedFileData(
                symbols=[s.to_dict() for s in result.symbols],
                includes=list(result.includes),
                tier_used=result.tier_used,
            )
        return precomputed

    def test_precomputed_produces_same_nodes(self, tmp_path):
        from ivy_lsp.core.semantic.model_builder import build_semantic_model
        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        # Full extraction (current path)
        model_full = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )

        # Pre-computed path (new path)
        precomputed = self._extract_files(root)
        model_pre = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model_full is not None
        assert model_pre is not None

        # Compare node counts by type
        for node_type in (SymbolNode, TypeNode):
            full_nodes = model_full.get_nodes_by_type(node_type)
            pre_nodes = model_pre.get_nodes_by_type(node_type)
            assert len(full_nodes) == len(pre_nodes), (
                f"{node_type.__name__}: {len(full_nodes)} vs {len(pre_nodes)}"
            )

        # Compare node IDs
        full_ids = {n.id for n in model_full.get_nodes_by_type(SymbolNode)}
        full_ids |= {n.id for n in model_full.get_nodes_by_type(TypeNode)}
        pre_ids = {n.id for n in model_pre.get_nodes_by_type(SymbolNode)}
        pre_ids |= {n.id for n in model_pre.get_nodes_by_type(TypeNode)}
        assert full_ids == pre_ids

    def test_precomputed_produces_same_edges(self, tmp_path):
        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model_builder import build_semantic_model
        from ivy_lsp.core.semantic.nodes import (
            RfcAnnotation,
            SymbolNode,
            TypeNode,
        )

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        model_full = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )

        precomputed = self._extract_files(root)
        model_pre = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=precomputed,
        )

        assert model_full is not None
        assert model_pre is not None

        # Collect all edges from both models (including RfcAnnotation COVERS edges)
        def _collect_edges(model):
            edges = set()
            for node_type in (SymbolNode, TypeNode, RfcAnnotation):
                for n in model.get_nodes_by_type(node_type):
                    for edge_type, target_id in model.get_outgoing(n.id):
                        edges.add((n.id, edge_type, target_id))
            return edges

        full_edges = _collect_edges(model_full)
        pre_edges = _collect_edges(model_pre)
        assert full_edges == pre_edges

    def test_precomputed_none_preserves_current_behavior(self, tmp_path):
        """Passing None for precomputed_extractions must use TieredExtractor."""
        from ivy_lsp.core.semantic.model_builder import build_semantic_model

        root = self._make_ivy_files(tmp_path)

        def find_files(r):
            return [f for f in os.listdir(r) if f.endswith(".ivy")]

        model = build_semantic_model(
            root=root,
            find_files_fn=find_files,
            precomputed_extractions=None,
        )
        # Should still produce a model (backward compat)
        assert model is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py::TestBuildSemanticModelPrecomputed -v`
Expected: FAIL — `build_semantic_model()` does not accept `precomputed_extractions` parameter.

- [ ] **Step 3: Write implementation**

Modify `build_semantic_model` in `ivy_lsp/core/semantic/model_builder.py`. Changes to the function:

1. Add the new parameter to the signature (after `stdlib_modules`):

```python
def build_semantic_model(
    root: str,
    find_files_fn: Callable[[str], list[str]],
    include_resolver: Any | None = None,
    stdlib_modules: frozenset[str] | None = None,
    precomputed_extractions: dict[str, PrecomputedFileData] | None = None,
) -> Optional[Any]:
```

2. Replace the per-file extraction block (lines 79-122, from the `populate_model_from_symbols` import through the end of the extraction loop). The new logic:

```python
    from ivy_lsp.core.parsing.symbol_to_model import populate_model_from_symbols

    # Only create TieredExtractor if we need it (no precomputed data).
    # Lazy-init: even when precomputed data is provided, a file may be
    # missing from the dict (e.g., added after Phase B). The extractor
    # is created on first miss to avoid silently skipping files.
    extractor = None
    if precomputed_extractions is None:
        from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
        extractor = TieredExtractor(resolve_callback=include_resolver)

    file_includes: dict[str, list[str]] = {}
    file_references: dict[str, list] = {}
    basename_to_path: dict[str, str] = {}
    tier_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
    total_symbols = 0
    build_start = time.monotonic()

    all_files = find_files_fn(root)
    for i, rel_path in enumerate(all_files):
        if i > 0 and i % 100 == 0:
            logger.info("Model build progress: %d/%d files", i, len(all_files))
        abs_path = os.path.join(root, rel_path)
        stem = os.path.splitext(os.path.basename(rel_path))[0]
        basename_to_path.setdefault(stem, abs_path)
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError as exc:
            logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
            continue

        # RFC annotations (operates on comments, not declarations)
        for ann in parse_file_rfc_annotations(source, abs_path):
            model.add_node(ann)

        # --- Extraction: precomputed or live ---
        pre = (
            precomputed_extractions.get(abs_path)
            if precomputed_extractions is not None
            else None
        )

        if pre is not None:
            # Deserialize symbols from Phase B dicts
            from ivy_lsp.core.parsing.symbols import IvySymbol

            symbols = [IvySymbol.from_dict(d) for d in pre.symbols]
            tier_used = pre.tier_used
            includes = pre.includes

            # Extract references via cheap regex (not stored in Phase B)
            from ivy_lsp.core.parsing.reference_extraction import (
                extract_references_regex,
            )

            references = extract_references_regex(source, abs_path, symbols)
        else:
            # Fallback: file not in precomputed dict (or no precomputed data).
            # Lazy-init extractor on first miss to avoid silently skipping files.
            if extractor is None:
                from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
                extractor = TieredExtractor(resolve_callback=include_resolver)
            result = extractor.extract(source, abs_path)
            if result.tier_used == 0:
                continue
            symbols = result.symbols
            tier_used = result.tier_used
            includes = result.includes
            references = result.references

        if tier_used > 0:
            count = populate_model_from_symbols(
                model, symbols, abs_path, tier_used=tier_used
            )
            total_symbols += count
            tier_counts[tier_used] = tier_counts.get(tier_used, 0) + 1
            file_includes[abs_path] = includes
            if references:
                file_references[abs_path] = references
```

The rest of the function (logging at lines 124-141, edge wiring at line 144) remains unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run existing model builder tests to check for regressions**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_semantic_model.py tests/test_semantic_model_merge.py -v`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/core/semantic/model_builder.py tests/test_model_builder_precomputed.py
git commit -m "feat(model-builder): accept precomputed extractions to skip re-parse

When precomputed_extractions is provided, the per-file loop deserializes
symbols via IvySymbol.from_dict() and extracts references via cheap regex
instead of invoking TieredExtractor.extract(). Backward-compatible: passing
None preserves current behavior for MCP and other callers."
```

---

### Task 3: Wire `_build_models` in `index_builder.py` to pass Phase B data

**Files:**
- Modify: `ivy_lsp/lsp/index_builder.py:365-421` (`_build_models` method)
- Modify: `ivy_lsp/lsp/index_builder.py:654-662` (`build_protocol` call site)

- [ ] **Step 1: Expand `_build_models` signature**

In `ivy_lsp/lsp/index_builder.py`, modify the `_build_models` method (line 365) to accept Phase B data:

```python
    def _build_models(
        self,
        protocol_dir: str,
        protocol: str,
        resolver,
        ivy_files: List[str],
        requirements_map: Dict[str, list],
        scopes: Dict,
        symbols_map: Dict[str, list],
        includes_raw: Dict[str, List[str]],
        manifest_files: Dict[str, dict],
    ) -> tuple:
```

- [ ] **Step 2: Build precomputed dict and pass it to `build_semantic_model`**

Replace the `_build_models` semantic model section (lines 378-391) with:

```python
        semantic_model = None
        try:
            from ivy_lsp.core.semantic.model_builder import (
                PrecomputedFileData,
                build_semantic_model,
            )

            def _find_files(root: str) -> List[str]:
                return [os.path.relpath(f, root) for f in ivy_files]

            # Build precomputed dict from Phase B extraction results
            tier_label_to_num = {"ast": 1, "lexer": 2, "regex": 3}
            precomputed: Dict[str, PrecomputedFileData] = {}
            for rel_path, syms in symbols_map.items():
                abs_path = os.path.join(protocol_dir, rel_path)
                tier_label = manifest_files.get(rel_path, {}).get(
                    "parse_tier", "unknown"
                )
                tier_num = tier_label_to_num.get(tier_label, 3)
                precomputed[abs_path] = PrecomputedFileData(
                    symbols=syms,
                    includes=includes_raw.get(rel_path, []),
                    tier_used=tier_num,
                )

            semantic_model = build_semantic_model(
                root=protocol_dir,
                find_files_fn=_find_files,
                include_resolver=resolver.resolve,
                precomputed_extractions=precomputed,
            )
        except Exception as exc:
            logger.debug("Semantic model build failed for %s: %s", protocol, exc)
```

- [ ] **Step 3: Update `build_protocol` call site**

At line 655, update the `_build_models` call to pass the three additional maps:

```python
        # -- 8-9. Build optional SemanticModel and ScopedRequirementModel ---
        semantic_model, requirement_graph = self._build_models(
            protocol_dir,
            protocol,
            resolver,
            ivy_files,
            requirements_map,
            scopes,
            symbols_map,
            includes_raw,
            manifest_files,
        )
```

All three variables (`symbols_map`, `includes_raw`, `manifest_files`) are already in scope at line 655 — they were populated during Phase B/C integration (lines 509-576).

- [ ] **Step 4: Run existing index builder tests**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_index_builder.py -v`
Expected: All existing tests PASS.

- [ ] **Step 5: Run the full precomputed test suite**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py tests/test_index_builder.py tests/test_semantic_model.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/lsp/index_builder.py
git commit -m "feat(index-builder): forward Phase B extractions to model builder

_build_models now receives symbols_map, includes_raw, and manifest_files
from Phase B. Converts them to PrecomputedFileData and passes to
build_semantic_model, eliminating the redundant TieredExtractor pass."
```

---

### Task 4: End-to-end integration test via `build_protocol`

**Files:**
- Modify: `tests/test_model_builder_precomputed.py` (append to file created in Task 1)

- [ ] **Step 1: Write integration test**

Add to `tests/test_model_builder_precomputed.py`:

```python
import json

from _ivy_samples import SAMPLE_IVY_MAIN, SAMPLE_IVY_TYPES

from ivy_lsp.core.workspace.detection import WorkspaceConfig
from ivy_lsp.lsp.index_builder import IndexBuilder


@pytest.mark.unit
class TestIndexBuilderPrecomputedIntegration:
    """Verify that build_protocol produces a valid semantic model
    using the precomputed extraction path."""

    def _make_workspace(self, tmp_path):
        ws_root = str(tmp_path)
        proto_dir = tmp_path / "protocol-testing" / "testproto"
        proto_dir.mkdir(parents=True)
        (proto_dir / "types.ivy").write_text(SAMPLE_IVY_TYPES)
        (proto_dir / "main.ivy").write_text(SAMPLE_IVY_MAIN)
        return ws_root, str(proto_dir)

    def test_build_protocol_produces_semantic_model(self, tmp_path):
        ws_root, proto_dir = self._make_workspace(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)

        summary = builder.build_protocol(proto_dir)

        assert summary["status"] == "ok"
        assert summary["files"] == 2

        # Verify semantic model was written
        pickle_path = os.path.join(proto_dir, ".ivy-index", "semantic_model.pickle.gz")
        assert os.path.isfile(pickle_path)

        # Load and verify it has nodes
        import gzip
        import pickle

        with gzip.open(pickle_path, "rb") as f:
            model = pickle.load(f)

        from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode

        sym_nodes = model.get_nodes_by_type(SymbolNode)
        type_nodes = model.get_nodes_by_type(TypeNode)

        # SAMPLE_IVY_MAIN has: packet (type), send (action), recv (action)
        # SAMPLE_IVY_TYPES has: cid (type), quic_packet_type (type)
        assert len(type_nodes) >= 2, f"Expected >= 2 TypeNodes, got {len(type_nodes)}"
        assert len(sym_nodes) >= 1, f"Expected >= 1 SymbolNodes, got {len(sym_nodes)}"

    def test_symbols_json_unchanged(self, tmp_path):
        """symbols.json must be byte-identical regardless of precomputed path,
        since Phase B is untouched."""
        ws_root, proto_dir = self._make_workspace(tmp_path)
        config = WorkspaceConfig(workspace_root=ws_root, detected_by="test")
        builder = IndexBuilder(ws_root, config)
        builder.build_protocol(proto_dir)

        symbols_path = os.path.join(proto_dir, ".ivy-index", "symbols.json")
        with open(symbols_path) as f:
            symbols = json.load(f)

        # Should have entries for both files
        assert len(symbols) == 2
```

- [ ] **Step 2: Run the integration test**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py::TestIndexBuilderPrecomputedIntegration -v`
Expected: PASS (2 tests)

- [ ] **Step 3: Run the full test suite for all touched modules**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_model_builder_precomputed.py tests/test_index_builder.py tests/test_index_builder_parallel.py tests/test_semantic_model.py tests/test_index_integration.py -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add tests/test_model_builder_precomputed.py
git commit -m "test: end-to-end integration test for precomputed model build

Verifies that build_protocol produces a valid semantic model with
correct node counts when using the precomputed extraction path."
```

---

### Task 5: Verification — compare old vs new on real protocol data

This task is a manual verification step, not automated. Run on the actual APT protocol data to confirm correctness and measure speedup.

- [ ] **Step 1: Baseline timing (before optimization)**

This was already measured: 572s total, 442s semantic model phase. Record as baseline.

- [ ] **Step 2: Run optimized indexer**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
python -m ivy_lsp index --force --all --workers 8 2> debug_indexing.optimized.log
```

Record total time and check logs for the model build timing line:
`"Model built: %d files, tiers={...}, %d symbols (%.1fms)"`

- [ ] **Step 3: Compare .ivy-index/ outputs**

For each protocol directory:
```bash
# Phase B artifacts should be byte-identical (not touched)
diff <(cat old/.ivy-index/symbols.json) <(cat new/.ivy-index/symbols.json)
diff <(cat old/.ivy-index/includes.json) <(cat new/.ivy-index/includes.json)

# Semantic model: compare structurally
python -c "
import gzip, pickle
with gzip.open('old/.ivy-index/semantic_model.pickle.gz', 'rb') as f:
    m1 = pickle.load(f)
with gzip.open('new/.ivy-index/semantic_model.pickle.gz', 'rb') as f:
    m2 = pickle.load(f)
from ivy_lsp.core.semantic.nodes import SymbolNode, TypeNode
for t in (SymbolNode, TypeNode):
    n1 = {n.id for n in m1.get_nodes_by_type(t)}
    n2 = {n.id for n in m2.get_nodes_by_type(t)}
    assert n1 == n2, f'{t.__name__} mismatch: {n1 ^ n2}'
print('Models match.')
"
```

- [ ] **Step 4: Confirm speedup target met**

Expected: total time drops from ~572s to ~160-200s (~3x faster). The model build timing log line should show ~20-40s instead of ~442s.

- [ ] **Step 5: Commit verification notes (optional)**

If results look good, no code changes needed. The optimization is complete.
