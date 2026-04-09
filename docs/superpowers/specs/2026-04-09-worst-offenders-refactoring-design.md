# Worst-Offenders Code Quality Refactoring

**Date**: 2026-04-09
**Scope**: 5 worst files in ivy-lsp by complexity and pain, plus cross-cutting deduplication
**Strategy**: Highest-pain first, functional decomposition, coverage-gated
**Breaking changes**: Full freedom on internal APIs; panther_ivy integration points updated as needed

## Context

Analysis of ivy-lsp (44,521 LOC source, ~150 files) identified 7 god classes/modules in the 11 largest files. This spec targets the 5 worst offenders, chosen by severity: longest methods, deepest nesting, highest responsibility count. The goal is to reduce each file to a manageable size through functional decomposition — breaking large functions into smaller module-level functions grouped by concern, consistent with the style already used in `extractor.py` (the best-structured file in the codebase).

### Constraints

- **Coverage-gated**: Before refactoring a module, ensure its tests pass. After refactoring, fix those tests before moving to the next module.
- **Functional decomposition**: No new class hierarchies. Extract plain functions into focused modules. The original module becomes a thin coordinator.
- **Execution order**: Highest pain first (Phase 1 = verification.py's 852-LOC closure, Phase 5 = server_setup.py).

## Phase 1: `mcp/tools/verification.py` (1,005 LOC)

### Problem

`register_verification_tools()` is a single function containing a `_CacheEntry` dataclass, 4 cache helper closures, and 5 tool handlers (`ivy_verify`, `ivy_compile`, `ivy_model_info`, `ivy_diagnostics`, `ivy_verification_dashboard`) sharing nonlocal cache state (`_verify_cache`, `_verify_cache_lock`, `_verify_in_flight`).

The `ivy_diagnostics` handler alone is 370 LOC of diagnostic layer orchestration (structural, lexer, semantic, coverage, pattern) that is logically unrelated to verification.

### Decomposition

#### New file: `mcp/tools/verification_cache.py` (~120 LOC)

Cache management as plain functions operating on a cache dict:

```
_CacheEntry (dataclass, module-level)
  - result: dict
  - file_mtime: float
  - include_mtimes: dict[str, float]

create_cache() -> tuple[dict, asyncio.Lock, set]
get_file_mtime(abs_path: str) -> float
get_include_mtimes(abs_path: str, basename_cache_fn) -> dict[str, float]
cache_is_fresh(entry: _CacheEntry, abs_path: str) -> bool
evict_oldest(cache: dict, max_size: int) -> None
cache_per_isolate_results(cache: dict, abs_path: str, raw_output: str, full_result: dict) -> None
get_cache_summary(cache: dict, max_size: int) -> dict
```

The `_CACHE_MAX_SIZE = 100` constant lives here. The `_ISOLATE_STATUS_RE` regex lives here (used by `cache_per_isolate_results`).

#### New file: `mcp/tools/diagnostics_tool.py` (~380 LOC)

The `ivy_diagnostics` handler and `ivy_verification_dashboard` extracted as-is:

```
register_diagnostic_tools(mcp, ctx, get_cache_summary_fn) -> None
  - ivy_diagnostics (handles modes: structural, full, collisions)
  - ivy_verification_dashboard (uses get_cache_summary_fn to read cache state
    without importing verification_cache directly — keeps diagnostic tools
    decoupled from verification internals)
```

The `_ASSERTION_RE` and `_BRACKET_TAG_RE` regexes move here (used only by the semantic diagnostics layer). After Phase 6 these will import from `core/patterns.py` instead.

#### Shrunk: `mcp/tools/verification.py` (~300 LOC)

```
register_verification_tools(mcp, ctx) -> None
  - Creates VerificationCache via create_cache()
  - Registers ivy_verify (~120 LOC, cache + subprocess orchestration)
  - Registers ivy_compile (~130 LOC, Docker fallback + subprocess)
  - Registers ivy_model_info (~50 LOC, auto-redirect + ivy_show)
  - Calls register_diagnostic_tools(mcp, ctx, get_cache_summary)
```

Scope resolution boilerplate replaced by `resolve_scope()` from `_helpers.py` (Phase 6).

## Phase 2: `lsp/index_builder.py` (1,152 LOC)

### Problem

`IndexBuilder.build_protocol()` is a 390-LOC method that orchestrates the entire index pipeline inline. `cli_index()` is 110 LOC mixing CLI argument parsing with builder invocation.

### Decomposition

#### New file: `lsp/index_cache.py` (~120 LOC)

Cache validation logic extracted from `build_protocol()` lines 487-578:

```
@dataclass
CachedIndex:
  manifest: dict | None
  symbols: dict | None
  includes_raw: dict | None
  exports: dict | None
  requirements: dict | None

load_cached_index(index_dir: str) -> CachedIndex
classify_files(
    ivy_files: list[str],
    protocol_dir: str,
    cached_index: CachedIndex,
    force: bool,
) -> tuple[dict, list[str], dict[str, str]]
    # Returns (cache_hits_map, files_to_extract, sha_for_file)
```

#### New file: `lsp/index_writer.py` (~150 LOC)

Index artifact persistence:

```
write_index_artifacts(
    index_dir: str,
    manifest: dict,
    symbols_map: dict,
    includes_raw: dict,
    exports_map: dict,
    requirements_map: dict,
    include_graph_data: dict | None,
    scopes: dict | None,
    semantic_model: Any | None,
    requirement_graph: Any | None,
) -> None

write_health_report(index_dir: str, health: dict) -> None
```

#### Shrunk: `lsp/index_builder.py` (~500 LOC)

`build_protocol()` becomes ~100 LOC calling:
1. `_create_protocol_resolver(protocol_dir)` (existing, stays)
2. `classify_files(ivy_files, cached_index, force)` (from index_cache.py)
3. `_extract_parallel()` or `_extract_one_file()` (existing, stay)
4. `_integrate_results(extraction_results)` (new ~50 LOC helper, extracted from lines 606-629)
5. Include graph + scope computation (existing inline, ~40 LOC)
6. `_build_models()` (existing, stays)
7. `write_index_artifacts(...)` (from index_writer.py)

`cli_index()` splits into `_parse_cli_args() -> argparse.Namespace` and `_run_cli_index(args)`.

`FileExtractionResult`, `_extract_one_file()`, tier constants stay in index_builder.py (picklability requirement for multiprocessing).

## Phase 3: `mcp/server.py` (1,193 LOC)

### Problem

`McpServerState` has 7+ responsibilities. `_build_model()` (84 LOC), `_build_requirement_graph()` (210 LOC), and `_write_model_to_index()` (111 LOC) are pure logic that doesn't need to live on the state object. `start_mcp()` (252 LOC) is a standalone function that inflates the file.

### Decomposition

#### New file: `mcp/model_builder.py` (~350 LOC)

Model and graph construction as pure functions:

```
build_mcp_model(
    workspace_context,
    root: str,
    include_paths: list[str],
    exclude_dirs: frozenset[str],
    resolver,
    find_ivy_files_fn,
) -> SemanticModel | None

build_requirement_graph(
    root: str,
    ivy_files: list[str],
    resolver,
    include_paths: list[str],
    exclude_dirs: frozenset[str],
    enrichment_adapter=None,
) -> ScopedRequirementModel | None

write_model_to_index(
    root: str,
    model,
    workspace_context,
    find_ivy_files_fn,
) -> None
```

Each function takes its dependencies as explicit arguments instead of reading `self.*`.

#### New file: `mcp/startup.py` (~280 LOC)

`start_mcp()` function and its internal helper `_register_all_tools()` extracted as-is. No structural changes, just a file move.

#### Shrunk: `mcp/server.py` (~400 LOC)

`McpServerState` retains:
- `__init__()` (~50 LOC, down from 82) — lazy builder lambdas call `model_builder.build_mcp_model(...)` etc.
- File finding: `find_ivy_files()`, `find_ivy_files_cached()`
- Basename cache: `get_basename_cache()`, `make_resolve_callback()`
- Lazy builder properties: `semantic_model`, `get_model()`, `get_model_status()`, `get_model_or_none()`, `requirement_graph`, `get_req_graph()`

Also stays: `_sidecar_monitor()`, `_IndexerProxy`, `_ServerProxy`, `_MCP_INSTRUCTIONS`.

## Phase 4: `lsp/commands.py` (796 LOC)

### Problem

`register()` is 297 LOC containing 3 nested async handlers that each repeat: validate tool on PATH, extract URI, detect isolate, resolve staging, build CWD, run subprocess, format result, refresh diagnostics.

### Decomposition

#### New file: `lsp/commands_helpers.py` (~300 LOC)

Shared helpers:

```
# Constants
TOOL_IVY_CHECK = "ivy_check"
TOOL_IVYC = "ivyc"
TOOL_IVY_SHOW = "ivy_show"

# Dataclass for resolved tool parameters
@dataclass
ToolParams:
  uri: str
  abs_path: str
  staged_path: str
  cwd: str
  isolate: str | None

# Extracted functions (currently scattered in commands.py)
find_tool(name: str) -> str | None
resolve_via_staging(server, filepath: str) -> str
detect_isolate_at_position(server, uri: str, position) -> str | None
resolve_tool_params(server, params) -> ToolParams
run_tool(server, cmd: list[str], cwd: str, ...) -> dict
refresh_open_diagnostics_sync(server) -> None
refresh_open_diagnostics(server) -> None
track_start(server, tool_name: str, uri: str) -> None
track_end(server, tool_name: str, uri: str, result: dict) -> None
```

The key extraction is `resolve_tool_params()` which centralizes the repeated preamble pattern from each handler into a single function.

#### Shrunk: `lsp/commands.py` (~350 LOC)

```
register(server) -> None
  - ivy_verify handler (~60 LOC): resolve_tool_params() + run_tool(ivy_check) + cache/counterexample logic
  - ivy_compile handler (~60 LOC): resolve_tool_params() + run_tool(ivyc, target=...) + Docker fallback
  - ivy_show_model handler (~50 LOC): resolve_tool_params() + run_tool(ivy_show, coi=...) + auto-redirect
```

## Phase 5: `lsp/server_setup.py` (822 LOC)

### Problem

`_prepopulate_from_offline_index()` is 173 LOC mixing pickle I/O, semantic model population, and fallback logic. `_setup_indexer()` is 151 LOC calling 5 helpers that are each 95-134 LOC.

### Decomposition

#### New file: `lsp/offline_index_loader.py` (~200 LOC)

Offline index loading as pure functions:

```
load_offline_indexes(workspace_root: str, workspace_context) -> dict
    # Returns {protocol: ProtocolIndex} with loaded pickles

merge_protocol_models(protocol_indexes: dict) -> SemanticModel | None
    # Iterates per-protocol pickles, merges into single model

populate_from_offline_index(
    workspace_context,
    protocol_indexes: dict,
) -> tuple[SemanticModel | None, dict | None]
    # Full flow: load + merge + populate workspace context
```

#### Shrunk: `lsp/server_setup.py` (~500 LOC)

- `_prepopulate_from_offline_index()` becomes a thin wrapper (~20 LOC) calling `populate_from_offline_index()` and setting `self._semantic_model`.
- `_setup_indexer()` shrinks from 151 to ~60 LOC — each of the 8 documented steps is a single function call.
- `_create_resolver()` shrinks from 125 to ~80 LOC by extracting workspace layer detection into `_detect_workspace_layers(ws_root, ws_folders)`.
- `_setup_analysis_pipeline()` shrinks from 134 to ~90 LOC by extracting adapter wiring into `_wire_adapters(pipeline, compiler_manager, enrichment)`.

## Phase 6: Cross-Cutting Cleanup

Small extractions that reduce duplication across the codebase. Done after the 5 main phases so the deduplication targets stable code.

### Addition to `mcp/tools/_helpers.py` (~40 LOC added)

```
def resolve_scope(ctx, scope: str, tool_name: str) -> resolved_scope | None:
    """Resolve scope and log warning if unknown. Replaces 7+ repeated blocks."""
    if not scope or getattr(ctx, "workspace_context", None) is None:
        return None
    resolved = ctx.workspace_context.get_test_scope(scope)
    if resolved is None:
        logger.warning("[%s] Unknown scope '%s'; proceeding without scoping", tool_name, scope)
    return resolved

def build_diagnostic_result(
    success: bool,
    diagnostics: list[dict],
    **extra,
) -> dict:
    """Build a standard diagnostic result dict with computed counts."""
    return {
        "success": success,
        "diagnostics": diagnostics,
        "diagnostic_count": len(diagnostics),
        "error_count": sum(1 for d in diagnostics if d.get("severity") == "error"),
        "warning_count": sum(1 for d in diagnostics if d.get("severity") == "warning"),
        "hint_count": sum(1 for d in diagnostics if d.get("severity") == "hint"),
        "info_count": sum(1 for d in diagnostics if d.get("severity") == "info"),
        **extra,
    }
```

### New file: `core/patterns.py` (~30 LOC)

Consolidated regex constants currently duplicated across 3 files:

```
import re

INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)", re.MULTILINE)
ASSERTION_RE = re.compile(r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE)
BRACKET_TAG_RE = re.compile(r"#\s*\[")
EXPORT_ACTION_RE = re.compile(r"^\s*export\s+action\s+([\w.]+)", re.MULTILINE)
MONITOR_RE = re.compile(r"^\s*(?:before|after|around)\s+([\w.]+)", re.MULTILINE)
```

Consumers in `lsp/diagnostics/compute.py`, `lsp/navigation/definition.py`, `mcp/tools/verification.py`, and `mcp/tools/diagnostics_tool.py` import from here instead of defining their own copies.

## Summary

| Phase | Target | Before (LOC) | After (LOC) | New files |
|-------|--------|-------------|------------|-----------|
| 1 | mcp/tools/verification.py | 1,005 | ~300 | verification_cache.py (~120), diagnostics_tool.py (~380) |
| 2 | lsp/index_builder.py | 1,152 | ~500 | index_cache.py (~120), index_writer.py (~150) |
| 3 | mcp/server.py | 1,193 | ~400 | model_builder.py (~350), startup.py (~280) |
| 4 | lsp/commands.py | 796 | ~350 | commands_helpers.py (~300) |
| 5 | lsp/server_setup.py | 822 | ~500 | offline_index_loader.py (~200) |
| 6 | Cross-cutting | scattered | — | core/patterns.py (~30), _helpers.py additions (~40) |

**Net effect**: 5 files shrunk from 4,968 LOC to ~2,050 LOC. 8 new focused modules totaling ~1,970 LOC. Net reduction of ~800 LOC from deduplication.

## Testing Strategy

Each phase is coverage-gated:
1. Run existing tests for the target module. Record pass/fail baseline.
2. Perform the decomposition.
3. Fix broken tests (import paths, renamed functions, moved classes).
4. Verify same tests pass before moving to the next phase.

No new test files are created as part of this refactoring. Test fixes are limited to updating imports and references to match the new module structure.

## Out of Scope

- Expanding `IvyServerProtocol` to cover all 20+ public properties (important but separate effort)
- Fixing the 175 bare `except Exception:` blocks (separate quality pass)
- Reducing the 77 private attribute access sites in LSP handlers (depends on protocol expansion)
- Refactoring `analysis_pipeline.py`, `pattern_library.py`, or `ast_to_symbols.py` (medium-priority, not worst offenders)
- Configuration externalization (tool timeouts, cache sizes to YAML)
