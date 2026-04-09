# Worst-Offenders Code Quality Refactoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the 5 worst-offender files in ivy-lsp into focused modules via functional decomposition, reducing the largest file from 1,005 LOC to ~300 LOC and eliminating ~800 LOC of duplication.

**Architecture:** Extract large nested functions and methods into standalone module-level functions in new files. The original modules become thin coordinators that import and call the extracted functions. No new class hierarchies — pure functional style matching `core/compilation/extractor.py`.

**Tech Stack:** Python 3.10+, asyncio, pygls, dataclasses. Tests via pytest.

**Spec:** `docs/superpowers/specs/2026-04-09-worst-offenders-refactoring-design.md`

**Working directory:** All paths relative to `ivy_lsp/` unless otherwise noted.

---

## File Structure

### New files to create

| File | Responsibility |
|------|---------------|
| `mcp/tools/verification_cache.py` | Verification result caching: entry dataclass, freshness checks, eviction, per-isolate caching |
| `mcp/tools/diagnostics_tool.py` | `ivy_diagnostics` and `ivy_verification_dashboard` MCP tool handlers |
| `lsp/index_cache.py` | Offline index cache validation: load cached artifacts, classify files as cache hits vs misses |
| `lsp/index_writer.py` | Index artifact persistence: write manifest, symbols, includes, exports, requirements, pickles |
| `mcp/model_builder.py` | Pure functions: build semantic model, build requirement graph, write model to index |
| `mcp/startup.py` | `start_mcp()` function and MCP app creation (moved from mcp/server.py) |
| `lsp/commands_helpers.py` | Shared LSP command helpers: tool resolution, staging, isolate detection, subprocess orchestration |
| `lsp/offline_index_loader.py` | Offline index deserialization: load pickles, merge per-protocol models, populate workspace context |
| `core/patterns.py` | Shared compiled regex patterns for Ivy syntax (include, assertion, export, monitor) |

### Files to modify

| File | Change |
|------|--------|
| `mcp/tools/verification.py` | Remove cache helpers, `ivy_diagnostics`, `ivy_verification_dashboard`; import from new modules |
| `mcp/tools/__init__.py` | Add import for `register_diagnostic_tools` from `diagnostics_tool.py` |
| `lsp/index_builder.py` | Extract cache validation and artifact writing; delegate to `index_cache.py` and `index_writer.py` |
| `mcp/server.py` | Extract `_build_model`, `_build_requirement_graph`, `_write_model_to_index`, `start_mcp` |
| `lsp/commands.py` | Extract helpers to `commands_helpers.py`; thin out handler bodies |
| `lsp/server_setup.py` | Extract `_prepopulate_from_offline_index` internals to `offline_index_loader.py` |
| `mcp/tools/_helpers.py` | Add `resolve_scope()` and `build_diagnostic_result()` |
| `lsp/diagnostics/compute.py` | Replace local `_INCLUDE_RE`, `_ASSERTION_RE` with imports from `core/patterns.py` |
| `lsp/navigation/definition.py` | Replace local `_INCLUDE_RE` with import from `core/patterns.py` |

### Test files to update (imports only)

| Test file | Import change |
|-----------|--------------|
| `tests/test_tools_verification.py` | `_ISOLATE_STATUS_RE` → import from `verification_cache.py`; `_CACHE_MAX_SIZE` → same |
| `tests/test_index_builder.py` | `_file_sha256` import stays (not moved) |
| `tests/test_index_builder_parallel.py` | No changes (imports `IndexBuilder`, `_extract_one_file`, `FileExtractionResult` — all stay) |
| `tests/test_commands.py` | `_detect_isolate_at_position`, `_find_tool`, `_run_tool` → import from `commands_helpers.py` |
| `tests/test_extract_param.py` | `_extract_param` → import from `commands_helpers.py` |
| 12 test files importing `start_mcp` | `from ivy_lsp.mcp.server import start_mcp` → `from ivy_lsp.mcp.startup import start_mcp` |

---

## Task 1: Baseline — Record Test Pass/Fail for Phase 1 Targets

**Files:**
- Read: `tests/test_tools_verification.py`
- Read: `tests/test_mcp_verification_wiring.py`

- [ ] **Step 1: Run verification-related tests and record baseline**

```bash
cd /Users/elniak/Documents/Documents/Work/Project/Protocol-Testing-Security/PANTHER/master/.claude/worktrees/lsp-to-claude/panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
source .venv/bin/activate
pytest tests/test_tools_verification.py tests/test_mcp_verification_wiring.py -v --tb=short 2>&1 | tail -30
```

Record the pass/fail count. This is the gate for Phase 1 completion.

- [ ] **Step 2: Commit baseline note**

No code changes — just record the baseline in a comment for reference.

---

## Task 2: Phase 1a — Create `mcp/tools/verification_cache.py`

**Files:**
- Create: `ivy_lsp/mcp/tools/verification_cache.py`

- [ ] **Step 1: Create `verification_cache.py` with cache functions**

Extract from `verification.py` lines 24-133 (the `_CacheEntry` dataclass, cache constants, and all cache helper functions):

```python
"""Verification result cache: entry storage, freshness checks, eviction."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CACHE_MAX_SIZE = 100

ISOLATE_STATUS_RE = re.compile(
    r"^\s*isolate\s+([\w.]+)\s*:\s*(PASS|FAIL|OK)\s*$", re.MULTILINE
)


@dataclass
class CacheEntry:
    """One cached verification result keyed by (abs_path, isolate)."""

    result: dict
    file_mtime: float
    include_mtimes: dict[str, float]


def create_cache() -> tuple[dict, asyncio.Lock, set]:
    """Create a fresh verification cache triple.

    Returns (cache_dict, async_lock, in_flight_set).
    """
    return {}, asyncio.Lock(), set()


def get_file_mtime(abs_path: str) -> float:
    """Get file mtime, returning 0.0 if file doesn't exist."""
    try:
        return os.path.getmtime(abs_path)
    except OSError:
        return 0.0


def get_include_mtimes(
    abs_path: str, basename_cache_fn: Any
) -> dict[str, float]:
    """Get mtimes for the file's transitive include closure."""
    mtimes: dict[str, float] = {}
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        for m in re.finditer(r"^\s*include\s+(\w+)", source, re.MULTILINE):
            inc_name = m.group(1)
            candidates = basename_cache_fn(inc_name)
            if candidates:
                inc_path = os.path.join(os.path.dirname(abs_path), candidates[0])
                mtimes[inc_path] = get_file_mtime(inc_path)
    except OSError:
        pass
    return mtimes


def cache_is_fresh(entry: CacheEntry, abs_path: str) -> bool:
    """Check if cached result is still fresh (no files changed)."""
    if get_file_mtime(abs_path) != entry.file_mtime:
        return False
    for inc_path, cached_mtime in entry.include_mtimes.items():
        if get_file_mtime(inc_path) != cached_mtime:
            return False
    return True


def evict_oldest(cache: dict, max_size: int = CACHE_MAX_SIZE) -> None:
    """Evict oldest cache entries when cache exceeds max_size."""
    while len(cache) > max_size:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key)


def cache_per_isolate_results(
    cache: dict,
    abs_path: str,
    raw_output: str,
    full_result: dict[str, Any],
) -> None:
    """Extract per-isolate status from verification output and cache each."""
    for m in ISOLATE_STATUS_RE.finditer(raw_output):
        iso_name = m.group(1)
        status = m.group(2)
        iso_key = (abs_path, iso_name)
        if iso_key not in cache:
            iso_success = status in ("PASS", "OK")
            iso_diags = [
                d
                for d in full_result.get("diagnostics", [])
                if iso_name in d.get("message", "")
                or iso_name in d.get("file", "")
            ]
            cache[iso_key] = CacheEntry(
                result={
                    "success": iso_success,
                    "diagnostics": iso_diags,
                    "diagnostic_count": len(iso_diags),
                    "error_summary": (
                        full_result.get("error_summary", "")
                        if not iso_success
                        else ""
                    ),
                    "duration_seconds": full_result.get(
                        "duration_seconds", 0
                    ),
                    "cached": False,
                    "isolate": iso_name,
                },
                file_mtime=get_file_mtime(abs_path),
                include_mtimes={},
            )
            evict_oldest(cache)


def get_cache_summary(
    cache: dict, max_size: int = CACHE_MAX_SIZE
) -> dict[str, Any]:
    """Return verification cache summary for dashboard."""
    verified: list[str] = []
    failed: list[str] = []
    seen: set[str] = set()
    for key, entry in cache.items():
        path = key[0] if isinstance(key, tuple) else str(key)
        if path in seen:
            continue
        seen.add(path)
        if entry.result.get("success"):
            verified.append(path)
        else:
            failed.append(path)
    return {
        "verified_files": verified,
        "failed_files": failed,
        "cache_size": len(cache),
        "cache_max": max_size,
    }
```

- [ ] **Step 2: Verify the new module imports cleanly**

```bash
cd /Users/elniak/Documents/Documents/Work/Project/Protocol-Testing-Security/PANTHER/master/.claude/worktrees/lsp-to-claude/panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
python -c "from ivy_lsp.mcp.tools.verification_cache import CacheEntry, create_cache, cache_is_fresh, evict_oldest, get_cache_summary, CACHE_MAX_SIZE, ISOLATE_STATUS_RE; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ivy_lsp/mcp/tools/verification_cache.py
git commit -m "refactor: extract verification cache to mcp/tools/verification_cache.py"
```

---

## Task 3: Phase 1b — Create `mcp/tools/diagnostics_tool.py`

**Files:**
- Create: `ivy_lsp/mcp/tools/diagnostics_tool.py`
- Read: `ivy_lsp/mcp/tools/verification.py:574-1005` (the `ivy_diagnostics` and `ivy_verification_dashboard` handlers)

- [ ] **Step 1: Read the full `ivy_diagnostics` and `ivy_verification_dashboard` handlers**

Read `ivy_lsp/mcp/tools/verification.py` lines 574-1005 to capture exact code.

- [ ] **Step 2: Create `diagnostics_tool.py`**

Move `ivy_diagnostics` (lines 574-947) and `ivy_verification_dashboard` (lines 975-1005) into a new module with its own `register_diagnostic_tools()` function. The function takes `mcp`, `ctx`, and `get_cache_summary_fn` as arguments (the callback decouples it from verification cache internals).

The file header:

```python
"""Diagnostic and verification dashboard MCP tools."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import error_response, inject_scope_metadata, safe_tool
from ivy_lsp.mcp.tools._helpers import validated_path_or_error

logger = logging.getLogger(__name__)

_ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)
_BRACKET_TAG_RE = re.compile(r"#\s*\[")


def register_diagnostic_tools(
    mcp: Any, ctx: Any, get_cache_summary_fn: Any
) -> None:
    """Register ivy_diagnostics and ivy_verification_dashboard tools."""

    # ... paste ivy_diagnostics handler (lines 574-947) as-is ...
    # ... paste ivy_verification_dashboard handler (lines 975-1005),
    #     replacing _get_cache_summary() calls with get_cache_summary_fn() ...
```

Copy the two handlers verbatim from `verification.py`, changing only:
- `_get_cache_summary()` → `get_cache_summary_fn()` (in `ivy_verification_dashboard`)
- Remove the `_get_cache_summary` definition (it stays in `verification.py` as a thin wrapper calling `verification_cache.get_cache_summary`)

- [ ] **Step 3: Verify the new module imports cleanly**

```bash
python -c "from ivy_lsp.mcp.tools.diagnostics_tool import register_diagnostic_tools; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/mcp/tools/diagnostics_tool.py
git commit -m "refactor: extract diagnostic tools to mcp/tools/diagnostics_tool.py"
```

---

## Task 4: Phase 1c — Rewire `verification.py` to use extracted modules

**Files:**
- Modify: `ivy_lsp/mcp/tools/verification.py`
- Modify: `ivy_lsp/mcp/tools/__init__.py:735,745`
- Modify: `tests/test_tools_verification.py:39,57`

- [ ] **Step 1: Rewrite `verification.py` to import from `verification_cache.py`**

Replace the cache-related code (lines 24-133) with imports:

```python
from ivy_lsp.mcp.tools.verification_cache import (
    CACHE_MAX_SIZE,
    CacheEntry,
    cache_is_fresh,
    cache_per_isolate_results,
    create_cache,
    evict_oldest,
    get_cache_summary,
    get_file_mtime,
    get_include_mtimes,
)
```

Inside `register_verification_tools()`:
- Replace the closure-scoped cache setup with:
  ```python
  _verify_cache, _verify_cache_lock, _verify_in_flight = create_cache()
  ```
- Remove the nested `_CacheEntry`, `_get_file_mtime`, `_get_include_mtimes`, `_cache_is_fresh`, `_evict_oldest_if_needed`, `_cache_per_isolate_results` definitions.
- Update all references from `_CacheEntry` → `CacheEntry`, `_get_file_mtime` → `get_file_mtime`, etc.
- Replace `_evict_oldest_if_needed()` calls with `evict_oldest(_verify_cache)`.
- Replace `_cache_is_fresh(entry, abs_path)` with `cache_is_fresh(entry, abs_path)`.
- Replace `_cache_per_isolate_results(abs_path, raw_output, result)` with `cache_per_isolate_results(_verify_cache, abs_path, raw_output, result)`.
- Update `_get_include_mtimes(abs_path)` calls to `get_include_mtimes(abs_path, lambda name: ctx.get_basename_cache().get(name, []))`.

- [ ] **Step 2: Remove `ivy_diagnostics` and `ivy_verification_dashboard` from `verification.py`**

Delete lines 574-1005 (the two handlers and `_get_cache_summary`).

Add at the end of `register_verification_tools()`:

```python
    from ivy_lsp.mcp.tools.diagnostics_tool import register_diagnostic_tools

    def _cache_summary() -> dict:
        return get_cache_summary(_verify_cache)

    ctx.get_verify_cache_summary = _cache_summary
    register_diagnostic_tools(mcp, ctx, _cache_summary)
```

Also remove `_ASSERTION_RE` and `_BRACKET_TAG_RE` from the top of `verification.py` (they moved to `diagnostics_tool.py`).

- [ ] **Step 3: Update test imports**

In `tests/test_tools_verification.py`, change:
```python
# Old:
from ivy_lsp.mcp.tools.verification import _ISOLATE_STATUS_RE
from ivy_lsp.mcp.tools.verification import _CACHE_MAX_SIZE
# New:
from ivy_lsp.mcp.tools.verification_cache import ISOLATE_STATUS_RE
from ivy_lsp.mcp.tools.verification_cache import CACHE_MAX_SIZE
```

Note: the names lost the leading underscore (they're now public module-level constants).

- [ ] **Step 4: Run Phase 1 tests to verify**

```bash
pytest tests/test_tools_verification.py tests/test_mcp_verification_wiring.py -v --tb=short 2>&1 | tail -30
```

Expected: Same pass/fail count as Task 1 baseline.

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/mcp/tools/verification.py tests/test_tools_verification.py
git commit -m "refactor: rewire verification.py to use verification_cache and diagnostics_tool"
```

---

## Task 5: Phase 2a — Baseline and create `lsp/index_cache.py`

**Files:**
- Create: `ivy_lsp/lsp/index_cache.py`
- Read: `ivy_lsp/lsp/index_builder.py:487-578`

- [ ] **Step 1: Run index builder tests and record baseline**

```bash
pytest tests/test_index_builder.py tests/test_index_builder_parallel.py -v --tb=short 2>&1 | tail -30
```

- [ ] **Step 2: Read `build_protocol()` cache validation section**

Read `ivy_lsp/lsp/index_builder.py` lines 487-602 to capture the exact cache logic.

- [ ] **Step 3: Create `index_cache.py`**

```python
"""Offline index cache validation: load artifacts, classify files as hits vs misses."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ivy_lsp.infra.utils.hashing import file_sha256 as _file_sha256

logger = logging.getLogger(__name__)


@dataclass
class CachedIndex:
    """Loaded artifacts from a previous .ivy-index/ build."""

    manifest: dict | None = None
    symbols: dict | None = None
    includes_raw: dict | None = None
    exports: dict | None = None
    requirements: dict | None = None


def _load_json(path: str) -> Any:
    """Load JSON from *path*, returning ``None`` on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def load_cached_index(index_dir: str) -> CachedIndex:
    """Load all cached index artifacts from *index_dir*."""
    return CachedIndex(
        manifest=_load_json(os.path.join(index_dir, "manifest.json")),
        symbols=_load_json(os.path.join(index_dir, "symbols.json")),
        includes_raw=_load_json(os.path.join(index_dir, "includes_raw.json")),
        exports=_load_json(os.path.join(index_dir, "exports.json")),
        requirements=_load_json(os.path.join(index_dir, "requirements.json")),
    )


@dataclass
class ClassifyResult:
    """Result of classifying files into cache hits vs extraction targets."""

    symbols_map: Dict[str, list] = field(default_factory=dict)
    includes_raw: Dict[str, List[str]] = field(default_factory=dict)
    exports_map: Dict[str, dict] = field(default_factory=dict)
    requirements_map: Dict[str, list] = field(default_factory=dict)
    manifest_files: Dict[str, dict] = field(default_factory=dict)
    tier_counts: Dict[str, int] = field(default_factory=dict)
    files_to_extract: List[str] = field(default_factory=list)
    sha_for_file: Dict[str, str] = field(default_factory=dict)
    cache_hits: int = 0
    cache_misses: int = 0


def classify_files(
    ivy_files: List[str],
    protocol_dir: str,
    cached: CachedIndex,
    force: bool,
    tier_labels: Dict[str, str] | None = None,
) -> ClassifyResult:
    """Split *ivy_files* into cache hits (populate result maps) and extraction targets.

    Args:
        ivy_files: Absolute paths to all .ivy files to process.
        protocol_dir: Absolute path to the protocol directory.
        cached: Previously loaded index artifacts.
        force: If True, skip cache entirely.
        tier_labels: Mapping of tier label constants for tier counting.

    Returns:
        ClassifyResult with populated maps for cache hits and lists for misses.
    """
    from ivy_lsp.lsp.index_builder import TIER_UNKNOWN

    result = ClassifyResult()
    if tier_labels:
        result.tier_counts = {k: 0 for k in tier_labels.values()}
    else:
        result.tier_counts = {}

    caches_valid = all(
        isinstance(c, dict)
        for c in [
            cached.symbols,
            cached.includes_raw,
            cached.exports,
            cached.requirements,
            cached.manifest,
        ]
    )

    if force or not caches_valid:
        result.files_to_extract = list(ivy_files)
        result.cache_misses = len(ivy_files)
        return result

    cached_sha256: Dict[str, str] = {}
    if isinstance(cached.manifest, dict):
        for rel_p, entry in cached.manifest.get("files", {}).items():
            if isinstance(entry, dict) and entry.get("sha256"):
                cached_sha256[rel_p] = entry["sha256"]

    for filepath in ivy_files:
        rel_path = os.path.relpath(filepath, protocol_dir)

        try:
            current_sha = _file_sha256(filepath)
        except OSError:
            current_sha = ""

        cached_hit = (
            current_sha
            and current_sha == cached_sha256.get(rel_path)
            and rel_path in cached.symbols
            and rel_path in cached.includes_raw
            and rel_path in cached.exports
            and rel_path in cached.requirements
            and rel_path in cached.manifest.get("files", {})
        )

        if cached_hit:
            result.cache_hits += 1
            result.symbols_map[rel_path] = cached.symbols[rel_path]
            result.includes_raw[rel_path] = cached.includes_raw[rel_path]
            result.exports_map[rel_path] = cached.exports[rel_path]
            result.requirements_map[rel_path] = cached.requirements[rel_path]
            result.manifest_files[rel_path] = cached.manifest["files"][rel_path]
            cached_tier = cached.manifest["files"][rel_path].get(
                "parse_tier", TIER_UNKNOWN
            )
            result.tier_counts[cached_tier] = result.tier_counts.get(cached_tier, 0) + 1
        else:
            result.cache_misses += 1
            result.files_to_extract.append(filepath)
            result.sha_for_file[filepath] = current_sha

    return result
```

- [ ] **Step 4: Verify import**

```bash
python -c "from ivy_lsp.lsp.index_cache import load_cached_index, classify_files, CachedIndex, ClassifyResult; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/lsp/index_cache.py
git commit -m "refactor: extract index cache validation to lsp/index_cache.py"
```

---

## Task 6: Phase 2b — Create `lsp/index_writer.py`

**Files:**
- Create: `ivy_lsp/lsp/index_writer.py`
- Read: `ivy_lsp/lsp/index_builder.py:724-778` (artifact writing section)

- [ ] **Step 1: Read the artifact writing section of `build_protocol()`**

Read `ivy_lsp/lsp/index_builder.py` lines 724-778.

- [ ] **Step 2: Create `index_writer.py`**

Extract the JSON/pickle writing logic. Read the exact code from `build_protocol()` and adapt to a standalone function. The function takes all the maps as arguments and writes them to `index_dir`.

```python
"""Index artifact persistence: write manifest, symbols, includes, exports, requirements."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def write_index_artifacts(
    index_dir: str,
    manifest: dict,
    symbols_map: Dict[str, list],
    includes_map: Dict[str, list],
    includes_raw: Dict[str, List[str]],
    exports_map: Dict[str, dict],
    requirements_map: Dict[str, list],
    scopes: Dict | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
) -> None:
    """Write all index artifacts to *index_dir*.

    Creates the directory if it doesn't exist. Writes JSON artifacts
    and optional pickle files for semantic model and requirement graph.
    """
    os.makedirs(index_dir, exist_ok=True)

    _write_json(os.path.join(index_dir, "manifest.json"), manifest)
    _write_json(os.path.join(index_dir, "symbols.json"), symbols_map)
    _write_json(os.path.join(index_dir, "includes.json"), includes_map)
    _write_json(os.path.join(index_dir, "includes_raw.json"), includes_raw)
    _write_json(os.path.join(index_dir, "exports.json"), exports_map)
    _write_json(os.path.join(index_dir, "requirements.json"), requirements_map)

    if scopes is not None:
        _write_scopes(index_dir, scopes)

    if semantic_model is not None:
        _write_pickle(index_dir, "semantic_model.pickle.gz", semantic_model)

    if requirement_graph is not None:
        _write_pickle(index_dir, "requirement_graph.pickle.gz", requirement_graph)


def write_health_report(index_dir: str, health: dict) -> None:
    """Write health report JSON to *index_dir*."""
    _write_json(os.path.join(index_dir, "health.json"), health)


def _write_json(path: str, data: Any) -> None:
    """Write JSON to *path*, logging errors."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except OSError:
        logger.warning("Failed to write %s", path, exc_info=True)


def _write_scopes(index_dir: str, scopes: Dict) -> None:
    """Write per-test scope files and _meta.json."""
    scopes_dir = os.path.join(index_dir, "scopes")
    os.makedirs(scopes_dir, exist_ok=True)
    meta: dict = {}
    for test_name, scope in scopes.items():
        scope_dict = scope.to_dict() if hasattr(scope, "to_dict") else scope
        _write_json(os.path.join(scopes_dir, f"{test_name}.json"), scope_dict)
        meta[test_name] = {
            "file": scope_dict.get("file", ""),
            "role": scope_dict.get("tester_role", "unknown"),
        }
    _write_json(os.path.join(scopes_dir, "_meta.json"), meta)


def _write_pickle(index_dir: str, filename: str, obj: Any) -> None:
    """Write a gzipped pickle to *index_dir*."""
    from ivy_lsp.infra.utils.serialization import write_locked_pickle

    write_locked_pickle(index_dir, filename, obj, logger)
```

- [ ] **Step 3: Verify import**

```bash
python -c "from ivy_lsp.lsp.index_writer import write_index_artifacts, write_health_report; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/lsp/index_writer.py
git commit -m "refactor: extract index artifact writer to lsp/index_writer.py"
```

---

## Task 7: Phase 2c — Rewire `index_builder.py`

**Files:**
- Modify: `ivy_lsp/lsp/index_builder.py`

- [ ] **Step 1: Read `build_protocol()` fully to plan precise edits**

Read `ivy_lsp/lsp/index_builder.py` lines 426-778 to see the complete method.

- [ ] **Step 2: Replace cache validation in `build_protocol()` with `classify_files()`**

Replace the cache-loading block (lines ~487-602) with:

```python
from ivy_lsp.lsp.index_cache import CachedIndex, load_cached_index, classify_files

cached = load_cached_index(index_dir_existing) if not self.force else CachedIndex()
cr = classify_files(
    ivy_files=ivy_files,
    protocol_dir=protocol_dir,
    cached=cached,
    force=self.force,
    tier_labels={TIER_AST: TIER_AST, TIER_LEXER: TIER_LEXER, TIER_REGEX: TIER_REGEX, TIER_UNKNOWN: TIER_UNKNOWN},
)
symbols_map = cr.symbols_map
includes_raw = cr.includes_raw
exports_map = cr.exports_map
requirements_map = cr.requirements_map
manifest_files = cr.manifest_files
tier_counts = cr.tier_counts
files_to_extract = cr.files_to_extract
sha_for_file = cr.sha_for_file
cache_hits = cr.cache_hits
cache_misses = cr.cache_misses
```

- [ ] **Step 3: Replace artifact writing with `write_index_artifacts()`**

Replace the writing block (lines ~724-769) with:

```python
from ivy_lsp.lsp.index_writer import write_index_artifacts

write_index_artifacts(
    index_dir=index_dir,
    manifest=manifest,
    symbols_map=symbols_map,
    includes_map=includes_map,
    includes_raw=includes_raw,
    exports_map=exports_map,
    requirements_map=requirements_map,
    scopes=scopes,
    semantic_model=semantic_model,
    requirement_graph=requirement_graph,
)
```

- [ ] **Step 4: Remove `_load_json()` from `IndexBuilder`**

Delete the `_load_json` static method (lines ~1017-1023) — it moved to `index_cache.py`.

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_index_builder.py tests/test_index_builder_parallel.py -v --tb=short 2>&1 | tail -30
```

Expected: Same pass/fail count as Task 5 baseline.

- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/lsp/index_builder.py
git commit -m "refactor: rewire index_builder.py to use index_cache and index_writer"
```

---

## Task 8: Phase 3a — Baseline and create `mcp/model_builder.py`

**Files:**
- Create: `ivy_lsp/mcp/model_builder.py`
- Read: `ivy_lsp/mcp/server.py:340-500,451-600`

- [ ] **Step 1: Run MCP server tests and record baseline**

```bash
pytest tests/test_mcp_index_bootstrap.py tests/test_tool_executor.py tests/test_sidecar_monitor.py -v --tb=short 2>&1 | tail -30
```

- [ ] **Step 2: Read `_build_model()`, `_build_requirement_graph()`, `_write_model_to_index()` from `McpServerState`**

Read `ivy_lsp/mcp/server.py` lines 340-600 to capture the exact implementations.

- [ ] **Step 3: Create `model_builder.py`**

Extract `_build_model()`, `_build_requirement_graph()`, and `_write_model_to_index()` as standalone functions. Each takes its dependencies as explicit arguments instead of reading `self.*`.

The function signatures should match the spec:

```python
"""Pure functions for building MCP semantic models and requirement graphs."""

from __future__ import annotations

import hashlib
import json as _json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


def build_mcp_model(
    workspace_context: Any,
    root: str,
    include_paths: list[str],
    exclude_dirs: frozenset[str],
    resolver: Any,
    find_ivy_files_fn: Any,
) -> Any:
    """Build a lightweight semantic model from workspace files.

    Tries two strategies:
    1. Offline index merge (per-protocol models from .ivy-index/)
    2. Full rebuild via TieredExtractor
    """
    # Copy the body of McpServerState._build_model() (read from mcp/server.py
    # in Step 2) and replace every self.X reference per the mapping above.
    # The method is ~84 LOC — paste it here verbatim with substitutions.
```

Read the full methods from server.py and adapt each `self.X` reference:
- `self.workspace_context` → `workspace_context` arg
- `self.root` → `root` arg
- `self._include_paths` → `include_paths` arg
- `self._effective_exclude_dirs` → `exclude_dirs` arg
- `self._resolver` → `resolver` arg
- `self.find_ivy_files` → `find_ivy_files_fn` arg

- [ ] **Step 4: Verify import**

```bash
python -c "from ivy_lsp.mcp.model_builder import build_mcp_model, build_requirement_graph, write_model_to_index; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/mcp/model_builder.py
git commit -m "refactor: extract model building to mcp/model_builder.py"
```

---

## Task 9: Phase 3b — Create `mcp/startup.py`

**Files:**
- Create: `ivy_lsp/mcp/startup.py`
- Read: `ivy_lsp/mcp/server.py:943-1193`

- [ ] **Step 1: Read `start_mcp()` fully**

Read `ivy_lsp/mcp/server.py` lines 943-1193.

- [ ] **Step 2: Move `start_mcp()` to `startup.py`**

Create `ivy_lsp/mcp/startup.py` with `start_mcp()` and any helper functions only used by it. Update its internal import of `McpServerState` to reference `ivy_lsp.mcp.server`.

```python
"""MCP server startup and app creation."""

from __future__ import annotations

# ... imports from the start_mcp section of server.py ...

from ivy_lsp.mcp.server import McpServerState

# ... start_mcp() function, verbatim from server.py ...
```

- [ ] **Step 3: Add re-export in `mcp/server.py` for backward compatibility during transition**

At the bottom of `mcp/server.py`, after removing `start_mcp`, add:

```python
# Re-export for callers that import from ivy_lsp.mcp.server
from ivy_lsp.mcp.startup import start_mcp  # noqa: F401
```

This ensures the 12 test files don't all need immediate updating.

- [ ] **Step 4: Verify import from both paths**

```bash
python -c "from ivy_lsp.mcp.startup import start_mcp; print('OK')"
python -c "from ivy_lsp.mcp.server import start_mcp; print('OK')"
```

Expected: Both print `OK`.

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/mcp/startup.py ivy_lsp/mcp/server.py
git commit -m "refactor: move start_mcp() to mcp/startup.py"
```

---

## Task 10: Phase 3c — Rewire `McpServerState` to use `model_builder.py`

**Files:**
- Modify: `ivy_lsp/mcp/server.py`

- [ ] **Step 1: Replace `_build_model()`, `_build_requirement_graph()`, `_write_model_to_index()` in `McpServerState`**

Replace each method body with a delegation to the extracted function:

```python
from ivy_lsp.mcp.model_builder import (
    build_mcp_model,
    build_requirement_graph,
    write_model_to_index,
)

# In McpServerState:

def _build_model(self):
    model = build_mcp_model(
        workspace_context=self.workspace_context,
        root=self.root,
        include_paths=self._include_paths,
        exclude_dirs=self._effective_exclude_dirs,
        resolver=self._resolver,
        find_ivy_files_fn=self.find_ivy_files,
    )
    if model is not None:
        write_model_to_index(
            root=self.root,
            model=model,
            workspace_context=self.workspace_context,
            find_ivy_files_fn=self.find_ivy_files,
        )
    return model

def _build_requirement_graph(self):
    return build_requirement_graph(
        root=self.root,
        ivy_files=self.find_ivy_files_cached(self.root),
        resolver=self._resolver,
        include_paths=self._include_paths,
        exclude_dirs=self._effective_exclude_dirs,
        enrichment_adapter=getattr(self, "_enrichment", None),
    )
```

Delete the old method bodies (the 84 LOC, 210 LOC, and 111 LOC original implementations).

- [ ] **Step 2: Remove `start_mcp()` from `server.py`**

Delete the `start_mcp()` function body (lines 943-1193). The re-export added in Task 9 Step 3 keeps existing imports working.

- [ ] **Step 3: Run Phase 3 tests**

```bash
pytest tests/test_mcp_index_bootstrap.py tests/test_tool_executor.py tests/test_sidecar_monitor.py -v --tb=short 2>&1 | tail -30
```

Expected: Same pass/fail as Task 8 baseline.

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/mcp/server.py
git commit -m "refactor: rewire McpServerState to delegate to model_builder.py"
```

---

## Task 11: Phase 4 — Extract `lsp/commands_helpers.py` and thin out `commands.py`

**Files:**
- Create: `ivy_lsp/lsp/commands_helpers.py`
- Modify: `ivy_lsp/lsp/commands.py`
- Modify: `tests/test_commands.py`
- Modify: `tests/test_extract_param.py`

- [ ] **Step 1: Run commands tests and record baseline**

```bash
pytest tests/test_commands.py tests/test_extract_param.py tests/test_command_dispatch.py tests/test_active_test_commands.py -v --tb=short 2>&1 | tail -30
```

- [ ] **Step 2: Read the helper functions from `commands.py`**

Read `ivy_lsp/lsp/commands.py` lines 1-500 to capture all helper functions before `register()`.

- [ ] **Step 3: Create `commands_helpers.py`**

Move all helper functions defined before `register()`:
- `_find_tool()` (line 26)
- `_resolve_via_staging()` (line 31)
- `_detect_isolate_at_position()` (line 55)
- `_find_enclosing_test()` (if exists)
- `_extract_param()` (if exists)
- `_run_tool()` (line 230)
- `_refresh_open_diagnostics_sync()` / `_refresh_open_diagnostics()`
- `_track_start()` / `_track_end()`
- Tool name constants

Also add the `ToolParams` dataclass and `resolve_tool_params()` function that centralizes the repeated preamble.

- [ ] **Step 4: Update `commands.py` to import from `commands_helpers.py`**

Replace deleted function definitions with:

```python
from ivy_lsp.lsp.commands_helpers import (
    _detect_isolate_at_position,
    _extract_param,
    _find_enclosing_test,
    _find_tool,
    _refresh_open_diagnostics,
    _refresh_open_diagnostics_sync,
    _resolve_via_staging,
    _run_tool,
    _track_end,
    _track_start,
)
```

- [ ] **Step 5: Update test imports**

In `tests/test_commands.py`, change:
```python
# Old:
from ivy_lsp.lsp.commands import _detect_isolate_at_position, _find_tool, _run_tool
# New:
from ivy_lsp.lsp.commands_helpers import _detect_isolate_at_position, _find_tool, _run_tool
```

In `tests/test_extract_param.py`, change:
```python
# Old:
from ivy_lsp.lsp.commands import _extract_param
# New:
from ivy_lsp.lsp.commands_helpers import _extract_param
```

- [ ] **Step 6: Run tests**

```bash
pytest tests/test_commands.py tests/test_extract_param.py tests/test_command_dispatch.py tests/test_active_test_commands.py -v --tb=short 2>&1 | tail -30
```

Expected: Same pass/fail as Step 1 baseline.

- [ ] **Step 7: Commit**

```bash
git add ivy_lsp/lsp/commands_helpers.py ivy_lsp/lsp/commands.py tests/test_commands.py tests/test_extract_param.py
git commit -m "refactor: extract command helpers to lsp/commands_helpers.py"
```

---

## Task 12: Phase 5 — Extract `lsp/offline_index_loader.py` and thin out `server_setup.py`

**Files:**
- Create: `ivy_lsp/lsp/offline_index_loader.py`
- Modify: `ivy_lsp/lsp/server_setup.py`

- [ ] **Step 1: Read `_prepopulate_from_offline_index()` fully**

Read `ivy_lsp/lsp/server_setup.py` lines 518-667.

- [ ] **Step 2: Create `offline_index_loader.py`**

Extract the pure logic from `_prepopulate_from_offline_index()` into standalone functions:

```python
"""Offline index deserialization: load pickles, merge per-protocol models."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def populate_from_offline_index(
    workspace_context: Any,
    indexer: Any,
    analysis_pipeline: Any = None,
) -> Optional[Any]:
    """Populate indexer from offline index artifacts.

    Mutates *indexer* in place (symbol table, include graph, exports,
    requirement graph). Returns the merged SemanticModel if one was
    loaded, or None.

    The requirement graph is set on ``indexer._requirement_graph``
    (matching the original server_setup.py behavior), NOT returned
    as a separate value.
    """
    protocol_indexes = getattr(workspace_context, "protocol_indexes", None)
    if not protocol_indexes:
        return None

    # Copy the body of _prepopulate_from_offline_index() (read from
    # server_setup.py in Step 1) and replace self.X references with
    # the function arguments (indexer, analysis_pipeline).
    # The method is ~150 LOC — paste it here verbatim with substitutions.
    # Key substitutions:
    #   self._indexer          → indexer
    #   self._analysis_pipeline → analysis_pipeline
    #   self._semantic_model = X → collect into local var, return at end
    #   self._indexer._requirement_graph = X → indexer._requirement_graph = X
    # Return the merged SemanticModel (or None).
```

Read the exact code from `server_setup.py` and adapt `self.X` references to function arguments.

- [ ] **Step 3: Update `server_setup.py` to delegate**

Replace `_prepopulate_from_offline_index()` body with:

```python
def _prepopulate_from_offline_index(self, ws_ctx) -> None:
    from ivy_lsp.lsp.offline_index_loader import populate_from_offline_index

    model = populate_from_offline_index(
        workspace_context=ws_ctx,
        indexer=self._indexer,
        analysis_pipeline=getattr(self, "_analysis_pipeline", None),
    )
    if model is not None:
        self._semantic_model = model
```

- [ ] **Step 4: Verify import**

```bash
python -c "from ivy_lsp.lsp.offline_index_loader import populate_from_offline_index; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Run a broad test sweep**

Server setup has no direct tests (it's a mixin tested indirectly). Run the full MCP and LSP test suites:

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -20
```

Expected: No new failures compared to overall baseline.

- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/lsp/offline_index_loader.py ivy_lsp/lsp/server_setup.py
git commit -m "refactor: extract offline index loading to lsp/offline_index_loader.py"
```

---

## Task 13: Phase 6a — Add `resolve_scope()` and `build_diagnostic_result()` to `_helpers.py`

**Files:**
- Modify: `ivy_lsp/mcp/tools/_helpers.py`

- [ ] **Step 1: Add the two helper functions**

Append to `ivy_lsp/mcp/tools/_helpers.py`:

```python
def resolve_scope(
    ctx: Any, scope: str, tool_name: str
) -> Any | None:
    """Resolve scope and log warning if unknown.

    Returns the resolved scope object, or None.
    Replaces 7+ repeated scope resolution blocks across tool modules.
    """
    if not scope or getattr(ctx, "workspace_context", None) is None:
        return None
    resolved = ctx.workspace_context.get_test_scope(scope)
    if resolved is None:
        logger.warning(
            "[%s] Unknown scope '%s'; proceeding without scoping",
            tool_name,
            scope,
        )
    return resolved


def build_diagnostic_result(
    success: bool,
    diagnostics: list[dict],
    **extra: Any,
) -> dict:
    """Build a standard diagnostic result dict with computed counts.

    Automatically computes diagnostic_count, error_count, warning_count,
    hint_count, and info_count from the diagnostics list.
    """
    return {
        "success": success,
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_count": sum(
            1 for d in diagnostics if d.get("severity") == "error"
        ),
        "warning_count": sum(
            1 for d in diagnostics if d.get("severity") == "warning"
        ),
        "hint_count": sum(
            1 for d in diagnostics if d.get("severity") == "hint"
        ),
        "info_count": sum(
            1 for d in diagnostics if d.get("severity") == "info"
        ),
        **extra,
    }
```

- [ ] **Step 2: Verify import**

```bash
python -c "from ivy_lsp.mcp.tools._helpers import resolve_scope, build_diagnostic_result; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ivy_lsp/mcp/tools/_helpers.py
git commit -m "refactor: add resolve_scope and build_diagnostic_result to _helpers.py"
```

---

## Task 14: Phase 6b — Create `core/patterns.py` and consolidate regex constants

**Files:**
- Create: `ivy_lsp/core/patterns.py`
- Modify: `ivy_lsp/lsp/diagnostics/compute.py`
- Modify: `ivy_lsp/lsp/navigation/definition.py`
- Modify: `ivy_lsp/mcp/tools/diagnostics_tool.py`

- [ ] **Step 1: Create `core/patterns.py`**

```python
"""Shared compiled regex patterns for Ivy language syntax.

Consolidates patterns previously duplicated across diagnostics/compute.py,
navigation/definition.py, and mcp/tools/verification.py.
"""

from __future__ import annotations

import re

INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)", re.MULTILINE)

ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)

BRACKET_TAG_RE = re.compile(r"#\s*\[")

EXPORT_ACTION_RE = re.compile(
    r"^\s*export\s+action\s+([\w.]+)", re.MULTILINE
)

MONITOR_RE = re.compile(
    r"^\s*(?:before|after|around)\s+([\w.]+)", re.MULTILINE
)
```

- [ ] **Step 2: Update `lsp/diagnostics/compute.py`**

Replace:
```python
_INCLUDE_RE = re.compile(r"^include\s+(\w+)", re.MULTILINE)
```
With:
```python
from ivy_lsp.core.patterns import INCLUDE_RE as _INCLUDE_RE
```

Replace:
```python
_ASSERTION_RE = re.compile(...)
```
With:
```python
from ivy_lsp.core.patterns import ASSERTION_RE as _ASSERTION_RE
```

- [ ] **Step 3: Update `lsp/navigation/definition.py`**

Replace:
```python
_INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)")
```
With:
```python
from ivy_lsp.core.patterns import INCLUDE_RE as _INCLUDE_RE
```

Note two behavioral changes vs. the originals:
- `navigation/definition.py`: old pattern lacked `re.MULTILINE`. The consolidated version adds it — correct for matching at start of any line.
- `diagnostics/compute.py`: old pattern was `r"^include\s+(\w+)"` (no leading `\s*`), matching only column-zero includes. The consolidated version adds `\s*`, broadening to indented includes. This is acceptable because Ivy `include` is a top-level directive, but verify no tests rely on the column-zero-only behavior.

- [ ] **Step 4: Update `mcp/tools/diagnostics_tool.py`**

Replace the local `_ASSERTION_RE` and `_BRACKET_TAG_RE` definitions with:

```python
from ivy_lsp.core.patterns import ASSERTION_RE as _ASSERTION_RE
from ivy_lsp.core.patterns import BRACKET_TAG_RE as _BRACKET_TAG_RE
```

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -20
```

Expected: No new failures.

- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/core/patterns.py ivy_lsp/lsp/diagnostics/compute.py ivy_lsp/lsp/navigation/definition.py ivy_lsp/mcp/tools/diagnostics_tool.py
git commit -m "refactor: consolidate duplicated regex patterns to core/patterns.py"
```

---

## Task 15: Final Validation

**Files:**
- Read: all modified files for sanity

- [ ] **Step 1: Run the complete test suite**

```bash
pytest tests/ --tb=short -q 2>&1 | tail -30
```

Record final pass/fail. Compare against the very first baseline from Task 1.

- [ ] **Step 2: Verify LOC reduction**

```bash
find ivy_lsp -name "*.py" -not -path "*__pycache__*" | xargs wc -l | sort -rn | head -20
```

Confirm the 5 target files are significantly smaller:
- `verification.py`: should be ~300 LOC (was 1,005)
- `index_builder.py`: should be ~500 LOC (was 1,152)
- `mcp/server.py`: should be ~400 LOC (was 1,193)
- `commands.py`: should be ~350 LOC (was 796)
- `server_setup.py`: should be ~500 LOC (was 822)

- [ ] **Step 3: Commit any remaining test fixes**

If any tests broke during the final sweep, fix imports and commit:

```bash
git add -u
git commit -m "fix: update remaining test imports after refactoring"
```

- [ ] **Step 4: Clean up re-export shim**

Remove the backward-compatibility re-export added in Task 9 Step 3 (the `from ivy_lsp.mcp.startup import start_mcp` in `mcp/server.py`). Update all 12 test files and `__main__.py` to import from `ivy_lsp.mcp.startup` directly.

```bash
# Update all test imports:
grep -rl "from ivy_lsp.mcp.server import start_mcp" tests/ | xargs sed -i '' 's/from ivy_lsp.mcp.server import start_mcp/from ivy_lsp.mcp.startup import start_mcp/g'
# Update __main__.py:
sed -i '' 's/from ivy_lsp.mcp.server import start_mcp/from ivy_lsp.mcp.startup import start_mcp/g' ivy_lsp/__main__.py
# Remove re-export from server.py
```

Then verify:

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -20
```

- [ ] **Step 5: Final commit**

```bash
git add -u
git commit -m "refactor: remove start_mcp re-export shim, update all imports to mcp.startup"
```
