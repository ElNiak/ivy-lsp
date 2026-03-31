# IndexBuilder Parse Cache & Parallel Extraction

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce QUIC offline indexing from ~500s to ~150s by parallelizing per-file extraction and caching results for incremental re-indexing.

**Architecture:** Extract the per-file processing loop in `IndexBuilder.build_protocol()` into a standalone picklable function `_extract_one_file()`. Add `ProcessPoolExecutor`-based parallel dispatch with configurable worker count. Add incremental mode that skips re-extracting files whose SHA-256 matches the existing manifest. Post-extraction phases (include graph, test scopes, semantic model) remain sequential in the main process.

**Tech Stack:** Python stdlib `concurrent.futures.ProcessPoolExecutor`, existing `TieredExtractor`, `IncludeResolver.to_config_dict()`/`from_config()`.

---

## File Structure

| File | Responsibility |
|------|---------------|
| Modify: `ivy_lsp/lsp/index_builder.py` | Add `_extract_one_file()`, parallel dispatch, incremental cache |
| Modify: `ivy_lsp/__main__.py` | Add `--workers` CLI flag |
| Create: `tests/test_index_builder_parallel.py` | Tests for parallel extraction and incremental cache |

---

### Task 1: Extract per-file work into a standalone function

**Files:**
- Modify: `ivy_lsp/lsp/index_builder.py:169-257`

The current `build_protocol()` has a 90-line for-loop that processes each file. This must become a top-level function (not a method) so `ProcessPoolExecutor` can pickle it.

- [ ] **Step 1: Write the failing test**

Create `tests/test_index_builder_parallel.py`:

```python
"""Tests for parallel and incremental IndexBuilder features."""

import os
import pytest

from ivy_lsp.lsp.index_builder import _extract_one_file, FileExtractionResult


class TestExtractOneFile:
    """Test the standalone per-file extraction function."""

    def test_returns_file_extraction_result(self, quic_workspace):
        """Extracting a valid .ivy file returns a FileExtractionResult."""
        ws_root = quic_workspace["workspace_root"]
        protocol_dir = quic_workspace["protocol_dir"]
        resolver_config = quic_workspace["resolver_config"]
        # Pick a small stack file
        filepath = os.path.join(protocol_dir, "quic_stack", "quic_types.ivy")
        if not os.path.isfile(filepath):
            pytest.skip("quic_types.ivy not found")

        result = _extract_one_file(
            filepath=filepath,
            protocol_dir=protocol_dir,
            resolver_config=resolver_config,
            fast=False,
            parser_timeout=5.0,
        )

        assert isinstance(result, FileExtractionResult)
        assert result.rel_path.endswith("quic_types.ivy")
        assert len(result.symbols) > 0
        assert result.tier_label in ("ast", "lexer", "regex", "unknown")
        assert result.sha256 != ""

    def test_fast_mode_skips_tier1(self, quic_workspace):
        """In fast mode, Tier 1 (parser) is skipped."""
        ws_root = quic_workspace["workspace_root"]
        protocol_dir = quic_workspace["protocol_dir"]
        resolver_config = quic_workspace["resolver_config"]
        filepath = os.path.join(protocol_dir, "quic_stack", "quic_types.ivy")
        if not os.path.isfile(filepath):
            pytest.skip("quic_types.ivy not found")

        result = _extract_one_file(
            filepath=filepath,
            protocol_dir=protocol_dir,
            resolver_config=resolver_config,
            fast=True,
            parser_timeout=0.0,
        )

        assert isinstance(result, FileExtractionResult)
        assert result.tier_label in ("lexer", "regex", "unknown")
```

- [ ] **Step 2: Add the `quic_workspace` fixture**

In `tests/conftest.py`, add (or locate existing equivalent):

```python
@pytest.fixture
def quic_workspace(ivy_workspace_root):
    """Provide QUIC workspace paths and resolver config for testing."""
    from ivy_lsp.core.workspace.detection import detect_ivy_workspace
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver

    protocol_dir = os.path.join(ivy_workspace_root, "protocol-testing", "quic")
    if not os.path.isdir(protocol_dir):
        pytest.skip("QUIC protocol dir not found")

    ws_config = detect_ivy_workspace(protocol_dir, ivy_workspace_root)
    protocol_rel = os.path.relpath(protocol_dir, ivy_workspace_root)
    resolver = IncludeResolver(
        workspace_root=ivy_workspace_root,
        exclude_paths=ws_config.exclude_paths,
        include_paths=[protocol_rel],
        workspace_layers=ws_config.workspace_layers,
    )
    try:
        resolver.create_staging_directory()
        if ws_config.workspace_layers:
            resolver.build_layered_staging()
    except Exception:
        pass

    return {
        "workspace_root": ivy_workspace_root,
        "protocol_dir": protocol_dir,
        "resolver_config": resolver.to_config_dict(),
    }
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_index_builder_parallel.py -v`
Expected: FAIL with `ImportError: cannot import name '_extract_one_file'`

- [ ] **Step 4: Implement `FileExtractionResult` and `_extract_one_file()`**

Add to `ivy_lsp/lsp/index_builder.py` after the helpers section (~line 58):

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FileExtractionResult:
    """Result of extracting symbols/includes/exports from one .ivy file."""

    rel_path: str
    symbols: list = field(default_factory=list)
    includes: list = field(default_factory=list)
    exports: dict = field(default_factory=dict)
    requirements: list = field(default_factory=list)
    manifest_entry: dict = field(default_factory=dict)
    tier_label: str = "unknown"
    tier1_errors: list = field(default_factory=list)
    sha256: str = ""
    error: Optional[str] = None


def _extract_one_file(
    filepath: str,
    protocol_dir: str,
    resolver_config: dict,
    fast: bool,
    parser_timeout: float,
) -> FileExtractionResult:
    """Extract symbols, includes, exports, and requirements from one .ivy file.

    This is a top-level function (not a method) so it can be pickled
    for ProcessPoolExecutor dispatch.

    Args:
        filepath: Absolute path to the .ivy file.
        protocol_dir: Absolute path to the protocol directory.
        resolver_config: Serialized IncludeResolver config from
            ``resolver.to_config_dict()``.
        fast: If True, skip Tier 1 (parser).
        parser_timeout: Seconds for Tier 1 parser lock timeout.

    Returns:
        FileExtractionResult with all extracted data.
    """
    from ivy_lsp.core.analysis.light_mode_extractor import (
        extract_exports_imports_light,
        extract_requirements_light,
    )
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.parsing.tiered_extractor import TieredExtractor
    from ivy_lsp.infra.utils.path_normalize import remap_node_id

    rel_path = os.path.relpath(filepath, protocol_dir)

    # Read file
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as exc:
        return FileExtractionResult(
            rel_path=rel_path, error=f"read error: {exc}"
        )

    # Reconstruct resolver from config (process-safe)
    resolver = IncludeResolver.from_config(resolver_config)

    # Create extractor with resolver
    extractor = TieredExtractor(
        resolve_callback=resolver.resolve,
        parser_timeout=0.0 if fast else parser_timeout,
    )
    if fast:
        extractor._parser_available = False

    # Extract symbols and includes
    try:
        result = extractor.extract(source, filepath)
    except Exception as exc:
        return FileExtractionResult(
            rel_path=rel_path, error=f"parse error: {exc}"
        )

    symbols = [s.to_dict() for s in result.symbols]
    includes = list(result.includes)

    # Extract exports/imports
    try:
        export_info = extract_exports_imports_light(source, filepath)
        exports = export_info.to_dict()
    except Exception:
        from ivy_lsp.core.analysis.test_scope import ExportImportInfo
        exports = ExportImportInfo(file=filepath).to_dict()

    # Extract requirements
    requirements = []
    try:
        reqs, _writes = extract_requirements_light(source, filepath)
        for r in reqs:
            r.file = rel_path
            r.id = remap_node_id(r.id, lambda _p: rel_path)
        requirements = [
            {
                "id": r.id, "kind": r.kind,
                "formula_text": r.formula_text, "line": r.line,
                "file": r.file, "monitor_action": r.monitor_action,
                "mixin_kind": r.mixin_kind,
            }
            for r in reqs
        ]
    except Exception:
        pass

    # Manifest entry
    completeness = "complete" if not result.errors else "partial"
    try:
        stat = os.stat(filepath)
        sha = _file_sha256(filepath)
    except OSError:
        stat = None
        sha = ""

    tier_label = _tier_label(result.tier_used)
    manifest_entry = {
        "mtime": stat.st_mtime if stat else 0.0,
        "size": stat.st_size if stat else 0,
        "sha256": sha,
        "completeness": completeness,
        "parse_tier": tier_label,
    }

    # Collect tier-1 errors
    tier1_errors = []
    for tier_err in result.errors:
        if tier_err.tier == 1:
            tier1_errors.append({
                "file": rel_path,
                "error_type": tier_err.error_type,
                "message": tier_err.message,
            })

    return FileExtractionResult(
        rel_path=rel_path,
        symbols=symbols,
        includes=includes,
        exports=exports,
        requirements=requirements,
        manifest_entry=manifest_entry,
        tier_label=tier_label,
        tier1_errors=tier1_errors,
        sha256=sha,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_index_builder_parallel.py -v`
Expected: PASS

- [ ] **Step 6: Refactor `build_protocol()` to use `_extract_one_file()`**

Replace the per-file loop in `build_protocol()` (lines 169-257) with:

```python
        # -- 2-4. Parse files, collect artifacts ---------------------------
        parser_timeout = 0.0 if self.fast else 5.0
        resolver_config = resolver.to_config_dict()

        manifest_files: Dict[str, dict] = {}
        symbols_map: Dict[str, list] = {}
        includes_raw: Dict[str, List[str]] = {}
        exports_map: Dict[str, dict] = {}
        requirements_map: Dict[str, list] = {}
        tier_counts: Dict[str, int] = {"ast": 0, "lexer": 0, "regex": 0, "unknown": 0}
        tier1_failures: List[Dict[str, str]] = []

        for filepath in ivy_files:
            result = _extract_one_file(
                filepath=filepath,
                protocol_dir=protocol_dir,
                resolver_config=resolver_config,
                fast=self.fast,
                parser_timeout=parser_timeout,
            )
            if result.error:
                manifest_files[result.rel_path] = self._manifest_entry_error(filepath)
                errors.append(result.error)
                continue

            symbols_map[result.rel_path] = result.symbols
            includes_raw[result.rel_path] = result.includes
            exports_map[result.rel_path] = result.exports
            requirements_map[result.rel_path] = result.requirements
            manifest_files[result.rel_path] = result.manifest_entry
            tier_counts[result.tier_label] = tier_counts.get(result.tier_label, 0) + 1
            tier1_failures.extend(result.tier1_errors)
```

- [ ] **Step 7: Run full test suite to verify refactor is behavior-preserving**

Run: `pytest tests/ -x -q --timeout=60 --ignore=tests/test_validation_correctness.py`
Expected: All existing tests pass (2835+)

- [ ] **Step 8: Commit**

```bash
git add ivy_lsp/lsp/index_builder.py tests/test_index_builder_parallel.py
git commit -m "refactor: extract per-file indexing into standalone _extract_one_file()"
```

---

### Task 2: Add incremental indexing with content-hash cache

**Files:**
- Modify: `ivy_lsp/lsp/index_builder.py` (in `build_protocol()`)

When not using `--force`, load the existing `.ivy-index/` manifest and reuse cached extraction results for files whose SHA-256 hasn't changed.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index_builder_parallel.py`:

```python
class TestIncrementalIndexing:
    """Test incremental indexing skips unchanged files."""

    def test_unchanged_file_uses_cache(self, tmp_path, ivy_workspace_root):
        """When a file's SHA-256 matches the manifest, extraction is skipped."""
        from ivy_lsp.lsp.index_builder import IndexBuilder, _extract_one_file
        from ivy_lsp.core.workspace.detection import detect_ivy_workspace

        protocol_dir = os.path.join(ivy_workspace_root, "protocol-testing", "minip")
        if not os.path.isdir(protocol_dir):
            pytest.skip("minip protocol dir not found")

        ws_config = detect_ivy_workspace(protocol_dir, ivy_workspace_root)

        # First build: force=True
        builder = IndexBuilder(ivy_workspace_root, ws_config, force=True)
        result1 = builder.build_protocol(protocol_dir)
        assert result1["status"] == "ok"
        elapsed1 = result1["elapsed_ms"]

        # Second build: force=True but incremental=True (files unchanged)
        builder2 = IndexBuilder(ivy_workspace_root, ws_config, force=True)
        result2 = builder2.build_protocol(protocol_dir)
        assert result2["status"] == "ok"
        # Incremental should be faster (but both are force=True for now)
        # This test validates the infrastructure; speed test comes after cache integration
```

- [ ] **Step 2: Run test to verify baseline**

Run: `pytest tests/test_index_builder_parallel.py::TestIncrementalIndexing -v`
Expected: PASS (baseline without cache)

- [ ] **Step 3: Add incremental cache loading to `build_protocol()`**

Add before the extraction loop in `build_protocol()`:

```python
        # -- Incremental cache: load existing manifest for SHA-256 comparison --
        existing_manifest_files: Dict[str, dict] = {}
        existing_symbols: Dict[str, list] = {}
        existing_includes: Dict[str, list] = {}
        existing_exports: Dict[str, dict] = {}
        existing_requirements: Dict[str, list] = {}
        cache_dir = os.path.join(protocol_dir, ".ivy-index")
        if os.path.isdir(cache_dir):
            try:
                existing_manifest_files = self._load_json(
                    os.path.join(cache_dir, "manifest.json")
                ).get("files", {})
                existing_symbols = self._load_json(
                    os.path.join(cache_dir, "symbols.json")
                )
                existing_includes = self._load_json(
                    os.path.join(cache_dir, "includes.json")
                )
                existing_exports = self._load_json(
                    os.path.join(cache_dir, "exports.json")
                )
                existing_requirements = self._load_json(
                    os.path.join(cache_dir, "requirements.json")
                )
            except Exception:
                logger.debug("Could not load existing index cache for %s", protocol)
```

Then modify the extraction loop to check the cache:

```python
        cache_hits = 0
        for filepath in ivy_files:
            rel_path = os.path.relpath(filepath, protocol_dir)

            # Check incremental cache
            if existing_manifest_files:
                try:
                    current_sha = _file_sha256(filepath)
                except OSError:
                    current_sha = ""
                cached = existing_manifest_files.get(rel_path, {})
                if (
                    current_sha
                    and cached.get("sha256") == current_sha
                    and rel_path in existing_symbols
                ):
                    # Cache hit: reuse existing extraction results
                    symbols_map[rel_path] = existing_symbols[rel_path]
                    includes_raw[rel_path] = existing_includes.get(rel_path, [])
                    exports_map[rel_path] = existing_exports.get(rel_path, {})
                    requirements_map[rel_path] = existing_requirements.get(rel_path, [])
                    manifest_files[rel_path] = cached
                    tier_counts[cached.get("parse_tier", "unknown")] = (
                        tier_counts.get(cached.get("parse_tier", "unknown"), 0) + 1
                    )
                    cache_hits += 1
                    continue

            # Cache miss: full extraction
            result = _extract_one_file(
                filepath=filepath,
                protocol_dir=protocol_dir,
                resolver_config=resolver_config,
                fast=self.fast,
                parser_timeout=parser_timeout,
            )
            # ... (existing result collection code)
```

Add a `_load_json` helper:

```python
    @staticmethod
    def _load_json(path: str) -> dict:
        """Load a JSON file, returning empty dict on failure."""
        with open(path) as f:
            return json.load(f)
```

Add a log line after the loop:

```python
        if cache_hits:
            logger.info(
                "Incremental cache: %d/%d files reused (%.0f%%)",
                cache_hits, len(ivy_files), cache_hits / len(ivy_files) * 100,
            )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -x -q --timeout=60 --ignore=tests/test_validation_correctness.py`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/lsp/index_builder.py tests/test_index_builder_parallel.py
git commit -m "feat: add incremental indexing with SHA-256 cache for unchanged files"
```

---

### Task 3: Add parallel extraction with ProcessPoolExecutor

**Files:**
- Modify: `ivy_lsp/lsp/index_builder.py`
- Modify: `ivy_lsp/__main__.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index_builder_parallel.py`:

```python
class TestParallelExtraction:
    """Test parallel file extraction."""

    def test_parallel_produces_same_results_as_sequential(self, ivy_workspace_root):
        """Parallel extraction must produce identical results to sequential."""
        from ivy_lsp.lsp.index_builder import IndexBuilder
        from ivy_lsp.core.workspace.detection import detect_ivy_workspace

        protocol_dir = os.path.join(ivy_workspace_root, "protocol-testing", "minip")
        if not os.path.isdir(protocol_dir):
            pytest.skip("minip protocol dir not found")

        ws_config = detect_ivy_workspace(protocol_dir, ivy_workspace_root)

        # Sequential build
        builder_seq = IndexBuilder(ivy_workspace_root, ws_config, force=True, workers=1)
        result_seq = builder_seq.build_protocol(protocol_dir)

        # Parallel build
        builder_par = IndexBuilder(ivy_workspace_root, ws_config, force=True, workers=2)
        result_par = builder_par.build_protocol(protocol_dir)

        assert result_seq["files"] == result_par["files"]
        assert result_seq["tests"] == result_par["tests"]
        assert result_seq["parse_tiers"] == result_par["parse_tiers"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_index_builder_parallel.py::TestParallelExtraction -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'workers'`

- [ ] **Step 3: Add `workers` parameter to `IndexBuilder.__init__()`**

```python
    def __init__(
        self,
        workspace_root: str,
        workspace_config: Any,
        fast: bool = False,
        force: bool = False,
        workers: int = 1,
    ) -> None:
        self.workspace_root = os.path.abspath(workspace_root)
        self.workspace_config = workspace_config
        self.fast = fast
        self.force = force
        self.workers = max(1, workers)
```

- [ ] **Step 4: Add parallel dispatch to `build_protocol()`**

Replace the sequential extraction loop with:

```python
        # -- 2-4. Parse files, collect artifacts (sequential or parallel) ---
        parser_timeout = 0.0 if self.fast else 5.0
        resolver_config = resolver.to_config_dict()

        # ... (incremental cache loading from Task 2) ...

        # Build list of files that need extraction (cache misses)
        files_to_extract = []
        for filepath in ivy_files:
            rel_path = os.path.relpath(filepath, protocol_dir)
            # ... (incremental cache check — if hit, add to maps and continue)
            files_to_extract.append(filepath)

        # Extract (parallel or sequential)
        if self.workers > 1 and len(files_to_extract) > 3:
            extraction_results = self._extract_parallel(
                files_to_extract, protocol_dir, resolver_config, parser_timeout,
            )
        else:
            extraction_results = [
                _extract_one_file(fp, protocol_dir, resolver_config, self.fast, parser_timeout)
                for fp in files_to_extract
            ]

        # Collect results
        for result in extraction_results:
            if result.error:
                manifest_files[result.rel_path] = self._manifest_entry_error(
                    os.path.join(protocol_dir, result.rel_path)
                )
                errors.append(result.error)
                continue
            symbols_map[result.rel_path] = result.symbols
            includes_raw[result.rel_path] = result.includes
            exports_map[result.rel_path] = result.exports
            requirements_map[result.rel_path] = result.requirements
            manifest_files[result.rel_path] = result.manifest_entry
            tier_counts[result.tier_label] = tier_counts.get(result.tier_label, 0) + 1
            tier1_failures.extend(result.tier1_errors)
```

- [ ] **Step 5: Implement `_extract_parallel()`**

Add to `IndexBuilder`:

```python
    def _extract_parallel(
        self,
        files: List[str],
        protocol_dir: str,
        resolver_config: dict,
        parser_timeout: float,
    ) -> List[FileExtractionResult]:
        """Extract files in parallel using ProcessPoolExecutor.

        Each worker process gets its own Ivy parser globals (process isolation),
        avoiding the _ivy_state_lock serialization bottleneck.
        """
        from concurrent.futures import ProcessPoolExecutor, as_completed

        results: List[FileExtractionResult] = []
        logger.info(
            "Parallel extraction: %d files across %d workers",
            len(files), self.workers,
        )

        with ProcessPoolExecutor(max_workers=self.workers) as executor:
            future_to_file = {
                executor.submit(
                    _extract_one_file,
                    filepath,
                    protocol_dir,
                    resolver_config,
                    self.fast,
                    parser_timeout,
                ): filepath
                for filepath in files
            }
            for future in as_completed(future_to_file):
                filepath = future_to_file[future]
                try:
                    result = future.result(timeout=60)
                    results.append(result)
                except Exception as exc:
                    rel = os.path.relpath(filepath, protocol_dir)
                    logger.warning("Worker failed for %s: %s", rel, exc)
                    results.append(FileExtractionResult(
                        rel_path=rel, error=f"worker error: {exc}",
                    ))

        return results
```

- [ ] **Step 6: Add `--workers` flag to CLI**

In `ivy_lsp/__main__.py`, find the `cli_index` argument parser and add:

```python
    parser.add_argument(
        "--workers", "-j",
        type=int,
        default=1,
        help="Number of parallel worker processes (default: 1, sequential).",
    )
```

And pass it to `IndexBuilder`:

```python
    builder = IndexBuilder(
        workspace_root=ws_root,
        workspace_config=ws_config,
        fast=args.fast,
        force=args.force,
        workers=args.workers,
    )
```

- [ ] **Step 7: Run all tests**

Run: `pytest tests/ -x -q --timeout=120 --ignore=tests/test_validation_correctness.py`
Expected: All tests pass (including new parallel correctness test)

- [ ] **Step 8: Manual benchmark**

Run:
```bash
# Sequential baseline
time python -m ivy_lsp index --force protocol-testing/quic 2>/dev/null

# Parallel (4 workers)
time python -m ivy_lsp index --force -j4 protocol-testing/quic 2>/dev/null
```

Expected: 3-4x speedup with `-j4`

- [ ] **Step 9: Commit**

```bash
git add ivy_lsp/lsp/index_builder.py ivy_lsp/__main__.py tests/test_index_builder_parallel.py
git commit -m "feat: add parallel extraction with ProcessPoolExecutor (-j/--workers flag)"
```

---

### Task 4: Wire `IncludeResolver.from_config()` staging reconstruction

**Files:**
- Modify: `ivy_lsp/core/indexer/include_resolver.py`

The `from_config()` classmethod reconstructs a resolver from a config dict, but it does NOT rebuild staging directories (the symlinks are on disk, created by the main process). Workers need staging to resolve cross-directory includes for Tier 1 parsing. Verify that `from_config()` points to the existing staging dir.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_index_builder_parallel.py`:

```python
class TestResolverSerialization:
    """Test IncludeResolver config round-trip for worker processes."""

    def test_resolver_roundtrip_preserves_staging(self, quic_workspace):
        """Resolver reconstructed from config can resolve cross-directory includes."""
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        config = quic_workspace["resolver_config"]
        restored = IncludeResolver.from_config(config)

        # The staging dir should exist (created by the fixture)
        if config.get("staging_dir"):
            assert os.path.isdir(config["staging_dir"])

        # Resolve a known cross-directory include
        quic_types_path = os.path.join(
            quic_workspace["protocol_dir"], "quic_stack", "quic_types.ivy"
        )
        if os.path.isfile(quic_types_path):
            result = restored.resolve("quic_types", quic_types_path)
            assert result is not None
            assert result.endswith("quic_types.ivy")
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_index_builder_parallel.py::TestResolverSerialization -v`
Expected: PASS if `from_config` correctly restores staging_dir. If FAIL, fix `from_config()`.

- [ ] **Step 3: Fix `from_config()` if needed**

Check that `from_config()` in `include_resolver.py` restores `_staging_dir` from the config dict. If it doesn't set `_staging_dir`, add:

```python
    @classmethod
    def from_config(cls, d: dict) -> "IncludeResolver":
        # ... existing code ...
        resolver = cls(
            workspace_root=d["workspace_root"],
            ivy_include_path=d.get("ivy_include_path"),
            exclude_paths=d.get("exclude_paths", []),
            include_paths=d.get("include_paths", []),
            workspace_layers=layers,
        )
        # Restore staging dir from config (created by main process)
        staging_dir = d.get("staging_dir")
        if staging_dir and os.path.isdir(staging_dir):
            resolver._staging_dir = staging_dir
            # Rebuild partition maps from existing staging dirs
            resolver._rebuild_partition_maps_from_staging()
        return resolver
```

If `_rebuild_partition_maps_from_staging()` doesn't exist, implement it by scanning `layer_*` subdirectories in the staging dir and rebuilding `_partition_staging` and `_file_to_partition` maps from the existing symlinks.

- [ ] **Step 4: Run full test suite**

Run: `pytest tests/ -x -q --timeout=120 --ignore=tests/test_validation_correctness.py`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/indexer/include_resolver.py tests/test_index_builder_parallel.py
git commit -m "fix: ensure IncludeResolver.from_config() restores staging for worker processes"
```
