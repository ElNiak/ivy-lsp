# ivy-lsp Package Boundary Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize ivy-lsp (~128 source files) from a flat structure into a 3-layer architecture: `core/` (shared domain logic), `lsp/` + `mcp/` (thin protocol shells), and `infra/` (cross-cutting infrastructure) -- with no functionality changes.

**Architecture:** Pure structural refactoring. Files are moved (preserving git blame via separate commits for moves vs content changes), imports are rewritten, and dependency rules (`infra/` -> nothing, `core/` -> `infra/` only, `lsp/`+`mcp/` -> `core/`+`infra/`) are enforced. The mcp_server.py monolith (1,253 LOC) is split into `mcp/server.py` + `mcp/context.py` after converting closures to a `McpServerState` class.

**Tech Stack:** Python 3.10+, pytest (~1,965 tests), pyright, `git mv` for blame-preserving moves

**Spec:** `docs/superpowers/specs/2026-03-25-ivy-lsp-package-redesign-design.md`

---

## Scope Check

This plan covers a single subsystem (ivy-lsp) restructured in 7 sequential phases. Each phase leaves the codebase in a working state with all tests passing.

## File Structure Overview

### Current Layout (pre-move)
```
ivy_lsp/                          # 128 source files, 15 __init__.py files
├── __init__.py, __main__.py
├── server.py, server_setup.py, bulk_orchestrator.py, index_builder.py
├── config.py, protocols.py, verification.py, pygls_patches.py
├── mcp_server.py (1,253 LOC monolith), mcp_bridge.py, mcp_sidecar.py, sidecar_client.py
├── adapters/                     -> core/adapters/
├── analysis/                     -> core/analysis/
├── compilation/                  -> core/compilation/
├── diagnostics/                  -> core/diagnostics/
├── features/                     -> split: lsp/* (handlers) + core/ (patterns, coverage_hints)
├── indexer/                      -> core/indexer/
├── observability/                -> infra/observability/ (minus LspLogHandler -> lsp/)
├── parsing/                      -> core/parsing/
├── rfc/                          -> core/rfc/
├── semantic/                     -> core/semantic/
├── tools/                        -> mcp/tools/
├── utils/                        -> infra/utils/ (minus structural_lint -> core/)
└── workspace/                    -> core/workspace/
```

### Target Layout (post-move)
```
ivy_lsp/
├── __init__.py                   # Version, top-level re-exports
├── __main__.py                   # CLI entry: dispatches to lsp/ or mcp/ mode
│
├── core/                         # Shared domain logic (may use lsprotocol enums)
│   ├── __init__.py
│   ├── protocols.py              # IvyServerProtocol (from protocols.py)
│   ├── verification.py           # Shared Ivy verification (from verification.py)
│   ├── coverage_hints.py         # Coverage hint computation (from features/coverage_hints.py)
│   ├── structural_lint.py        # Fast syntax validation (from utils/structural_lint.py)
│   ├── parsing/                  # (from parsing/)
│   │   ├── __init__.py
│   │   ├── symbols.py, ast_to_symbols.py, symbol_to_model.py
│   │   ├── tiered_extractor.py, parser_session.py, token_stream.py
│   │   └── fallback_scanner.py, fallback_parser.py
│   ├── semantic/                 # (from semantic/)
│   │   ├── __init__.py
│   │   ├── model.py, nodes.py, edges.py
│   │   ├── model_builder.py, analysis_pipeline.py
│   │   ├── rfc_annotations.py, snapshots.py
│   │   └── nct_core/             # (from semantic/nct_core/ if exists)
│   ├── analysis/                 # (from analysis/)
│   │   ├── __init__.py
│   │   ├── requirement_extractor.py, requirement_graph.py
│   │   ├── light_mode_extractor.py, lexer_requirement_extractor.py
│   │   ├── formula_analyzer.py, impl_block_parser.py
│   │   ├── test_scope.py, pattern_library.py
│   │   └── requirements/        # (if sub-package exists)
│   ├── indexer/                  # (from indexer/)
│   │   ├── __init__.py
│   │   ├── workspace_indexer.py, file_cache.py, include_resolver.py
│   │   ├── deep_indexer.py, parallel_indexer.py, scope_manager.py
│   │   └── symbols.py           # (if exists)
│   ├── compilation/              # (from compilation/)
│   │   ├── __init__.py
│   │   ├── compiler_manager.py, extractor.py, ir.py
│   │   ├── graph_enrichment.py, worker.py
│   │   └── nct_core/             # (if exists)
│   ├── diagnostics/              # (from diagnostics/)
│   │   ├── __init__.py
│   │   ├── codes.py, modes.py, rich_diagnostic.py
│   │   └── nct_core/             # (if exists)
│   ├── workspace/                # (from workspace/)
│   │   ├── __init__.py
│   │   ├── detection.py, context.py, active_workspace.py
│   │   └── session_overlay.py
│   ├── rfc/                      # (from rfc/)
│   │   ├── __init__.py
│   │   ├── fetcher.py, parser.py, staleness.py
│   │   └── nct_core/             # (if exists)
│   └── adapters/                 # (from adapters/)
│       ├── __init__.py
│       ├── protocols.py, compiler_adapter.py
│       ├── ast_enrichment_adapter.py, null_adapter.py
│       └── nct_core/             # (if exists)
│
├── lsp/                          # LSP protocol shell (thin adapter)
│   ├── __init__.py
│   ├── server.py                 # IvyLanguageServer class (from server.py)
│   ├── server_setup.py           # ServerSetupMixin (from server_setup.py)
│   ├── bulk_orchestrator.py      # BulkOrchestrationMixin (from bulk_orchestrator.py)
│   ├── index_builder.py          # Workspace indexing orchestrator (from index_builder.py)
│   ├── pygls_patches.py          # pygls library patches (from pygls_patches.py)
│   ├── lsp_log_handler.py        # LspLogHandler (extracted from observability/handlers.py)
│   ├── completion.py             # (from features/completion.py)
│   ├── rename.py                 # (from features/rename.py)
│   ├── code_action.py            # (from features/code_action.py)
│   ├── signature_help.py         # (from features/signature_help.py)
│   ├── document_symbols.py       # (from features/document_symbols.py)
│   ├── workspace_symbols.py      # (from features/workspace_symbols.py)
│   ├── document_highlight.py     # (from features/document_highlight.py)
│   ├── commands.py               # (from features/commands.py)
│   ├── commands_extended.py      # (from features/commands_extended.py)
│   ├── visualization.py          # (from features/visualization.py)
│   ├── viz_coverage.py           # (from features/viz_coverage.py)
│   ├── viz_graphs.py             # (from features/viz_graphs.py)
│   ├── viz_suggestions.py        # (from features/viz_suggestions.py)
│   ├── navigation/               # Navigation sub-package
│   │   ├── __init__.py
│   │   ├── definition.py         # (from features/definition.py)
│   │   ├── implementation.py     # (from features/implementation.py)
│   │   ├── references.py         # (from features/references.py)
│   │   ├── call_hierarchy.py     # (from features/call_hierarchy.py)
│   │   └── hover.py              # (from features/hover.py)
│   ├── diagnostics/              # Diagnostics sub-package
│   │   ├── __init__.py
│   │   ├── publisher.py          # RENAMED from features/diagnostics.py
│   │   └── compute.py            # RENAMED from features/diagnostic_compute.py
│   └── ui/                       # UI sub-package
│       ├── __init__.py
│       ├── code_lens.py          # (from features/code_lens.py)
│       ├── folding_range.py      # (from features/folding_range.py)
│       ├── selection_range.py    # (from features/selection_range.py)
│       ├── monitoring.py         # (from features/monitoring.py)
│       └── status.py             # (from features/status.py)
│
├── mcp/                          # MCP protocol shell (thin adapter)
│   ├── __init__.py
│   ├── server.py                 # start_mcp(), create_mcp_app() (from mcp_server.py)
│   ├── context.py                # ToolContext, McpServerState (extracted from mcp_server.py)
│   ├── bridge.py                 # (from mcp_bridge.py)
│   ├── sidecar.py                # (from mcp_sidecar.py)
│   ├── client.py                 # (from sidecar_client.py)
│   ├── tools/                    # (from tools/)
│   │   ├── __init__.py
│   │   ├── analysis.py, verification.py, traceability.py
│   │   ├── traceability_extraction.py, visualization.py
│   │   ├── workspace.py, quality.py, patterns.py
│   │   └── formatters/
│   │       ├── __init__.py
│   │       ├── primitives.py     # RENAMED from _primitives.py
│   │       ├── verification.py, traceability.py, visualization.py
│   │       └── nct_core/         # (if exists)
│   └── nct_core/                 # (if exists)
│
└── infra/                        # Cross-cutting infrastructure
    ├── __init__.py
    ├── config.py                 # (from config.py)
    ├── observability/            # (from observability/, minus LspLogHandler)
    │   ├── __init__.py
    │   ├── core.py, handlers.py, session.py
    │   └── nct_core/             # (if exists)
    └── utils/                    # (from utils/, minus structural_lint)
        ├── __init__.py
        ├── path_normalize.py, position_utils.py, symbol_resolver.py
        ├── async_subprocess.py, ivy_output.py
        ├── counterexample_parser.py, counterexample_formatter.py
        ├── scope_ranking.py, validation.py
        └── nct_core/             # (if exists)
```

---

## Phase 0: Pre-flight

### Task 0.1: Establish baseline and create git tag

**Files:**
- Read: `tests/`, `ivy_lsp/`

- [ ] **Step 1: Run the full test suite and record the baseline count**

```bash
cd /Users/elniak/Documents/Documents/Work/Project/Protocol-Testing-Security/PANTHER/master/.claude/worktrees/lsp-to-claude/panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
pytest tests/ -x --tb=short -q 2>&1 | tail -20
```
Expected: ~1,965 tests pass. Record the exact count for regression checking.

- [ ] **Step 2: Create a git tag for the pre-refactor baseline**

```bash
git tag pre-package-redesign -m "Baseline before 3-layer package redesign"
```

- [ ] **Step 3: Commit tag**

```bash
git push origin pre-package-redesign  # Only if user approves push
```

### Task 0.2: Build @patch() inventory

**Files:**
- Read: `tests/` (all files using `@patch` or `patch()`)
- Create: `docs/superpowers/plans/patch-inventory.md`

The @patch() string paths in tests are the single biggest risk in this refactoring. They target module paths that will change. This task inventories every one.

- [ ] **Step 1: Generate the @patch inventory**

```bash
cd ivy_lsp/../
grep -rn 'patch("ivy_lsp\.' tests/ --include="*.py" | \
  sed 's/.*patch("\(ivy_lsp\.[^"]*\)".*/\1/' | \
  sort -u > /tmp/patch-inventory.txt
cat /tmp/patch-inventory.txt
```

- [ ] **Step 2: Create the patch inventory document**

Create `docs/superpowers/plans/patch-inventory.md` with each patch target and its new path after the refactoring. Format:

```markdown
# @patch() Path Inventory

| Current Path | New Path | Phase | Test File(s) |
|---|---|---|---|
| ivy_lsp.mcp_server.sidecar_client | ivy_lsp.mcp.server.sidecar_client | 4 | test_lazy_bridge_integration.py, test_sidecar_monitor.py |
| ivy_lsp.mcp_server.shared_ivy_check | ivy_lsp.mcp.server.shared_ivy_check | 4 | test_mcp_verification_wiring.py |
| ivy_lsp.__main__._setup_log_rotation | ivy_lsp.__main__._setup_log_rotation | (unchanged) | test_index_cli.py |
| ivy_lsp.__main__.sys | ivy_lsp.__main__.sys | (unchanged) | test_index_cli.py |
| ivy_lsp.features.document_symbols.compute_document_symbols | ivy_lsp.lsp.document_symbols.compute_document_symbols | 3 | test_commands.py |
| ivy_lsp.parsing.fallback_scanner.fallback_scan | ivy_lsp.core.parsing.fallback_scanner.fallback_scan | 2 | test_task_3_2_diagnostics.py |
| ivy_lsp.parsing.ast_to_symbols.ast_to_symbols | ivy_lsp.core.parsing.ast_to_symbols.ast_to_symbols | 2 | test_deep_index_progress.py |
| ivy_lsp.sidecar_client._fetch_health | ivy_lsp.mcp.client._fetch_health | 4 | test_sidecar_client.py |
| ivy_lsp.index_builder.cli_index | ivy_lsp.lsp.index_builder.cli_index | 3 | test_index_cli.py |
```

- [ ] **Step 3: Commit the inventory**

```bash
git add docs/superpowers/plans/patch-inventory.md
git commit -m "docs: add @patch() path inventory for package redesign"
```

---

## Phase 1: Move infra/ (config, observability, utils)

**Risk:** Low | **Estimated import changes:** ~550

This phase moves the infrastructure layer first because it has zero dependencies on other ivy_lsp modules (dependency rule: `infra/` -> nothing). Everything else can import from `infra/`.

### Task 1.1: Create infra/ package skeleton

**Files:**
- Create: `ivy_lsp/infra/__init__.py`

- [ ] **Step 1: Write a test that imports from the new infra package**

```python
# tests/test_infra_skeleton.py
def test_infra_package_importable():
    """Verify the infra/ package exists and is importable."""
    import ivy_lsp.infra
    assert hasattr(ivy_lsp.infra, '__name__')
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_infra_skeleton.py -x -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'ivy_lsp.infra'`

- [ ] **Step 3: Create the infra package**

```python
# ivy_lsp/infra/__init__.py
"""Cross-cutting infrastructure for ivy-lsp."""
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_infra_skeleton.py -x -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/infra/__init__.py tests/test_infra_skeleton.py
git commit -m "feat: create infra/ package skeleton"
```

### Task 1.2: Move config.py -> infra/config.py (pure move)

**Files:**
- Move: `ivy_lsp/config.py` -> `ivy_lsp/infra/config.py`

- [ ] **Step 1: Move the file using git mv (preserves blame)**

```bash
git mv ivy_lsp/config.py ivy_lsp/infra/config.py
```

- [ ] **Step 2: Add a re-export shim at the old path**

This is a temporary shim so existing imports don't break during the phased migration. It will be removed in Phase 6 cleanup.

```python
# ivy_lsp/config.py
"""Backward-compat shim — will be removed in Phase 6."""
from ivy_lsp.infra.config import *  # noqa: F401,F403
from ivy_lsp.infra.config import get_config, reset_config, ServerConfig  # noqa: F401
```

- [ ] **Step 3: Run the full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline.

- [ ] **Step 4: Commit the pure move (separate from import rewrites)**

```bash
git add ivy_lsp/config.py ivy_lsp/infra/config.py
git commit -m "refactor: move config.py -> infra/config.py (with shim)"
```

### Task 1.3: Move observability/ -> infra/observability/ (pure move)

**Files:**
- Move: `ivy_lsp/observability/` -> `ivy_lsp/infra/observability/`

- [ ] **Step 1: Move the directory using git mv**

```bash
git mv ivy_lsp/observability ivy_lsp/infra/observability
```

- [ ] **Step 2: Add a re-export shim at the old path**

```python
# ivy_lsp/observability/__init__.py
"""Backward-compat shim — will be removed in Phase 6."""
from ivy_lsp.infra.observability import *  # noqa: F401,F403
```

Also create shim files for submodules that are directly imported:

```python
# ivy_lsp/observability/core.py
"""Backward-compat shim."""
from ivy_lsp.infra.observability.core import *  # noqa: F401,F403

# ivy_lsp/observability/handlers.py
"""Backward-compat shim."""
from ivy_lsp.infra.observability.handlers import *  # noqa: F401,F403

# ivy_lsp/observability/session.py
"""Backward-compat shim."""
from ivy_lsp.infra.observability.session import *  # noqa: F401,F403
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline.

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/observability/ ivy_lsp/infra/observability/
git commit -m "refactor: move observability/ -> infra/observability/ (with shims)"
```

### Task 1.4: Move utils/ -> infra/utils/ (pure move, minus structural_lint)

**Files:**
- Move: `ivy_lsp/utils/` -> `ivy_lsp/infra/utils/`
- Except: `ivy_lsp/utils/structural_lint.py` -> `ivy_lsp/core/structural_lint.py` (Phase 2)

- [ ] **Step 1: Move utils/ to infra/utils/**

```bash
git mv ivy_lsp/utils ivy_lsp/infra/utils
```

- [ ] **Step 2: Add a re-export shim at the old path**

Create `ivy_lsp/utils/` directory with shims:

```python
# ivy_lsp/utils/__init__.py
"""Backward-compat shim — will be removed in Phase 6."""
from ivy_lsp.infra.utils import *  # noqa: F401,F403
```

Create shims for commonly directly-imported submodules:
- `ivy_lsp/utils/path_normalize.py`
- `ivy_lsp/utils/position_utils.py`
- `ivy_lsp/utils/symbol_resolver.py`
- `ivy_lsp/utils/async_subprocess.py`
- `ivy_lsp/utils/ivy_output.py`
- `ivy_lsp/utils/counterexample_parser.py`
- `ivy_lsp/utils/counterexample_formatter.py`
- `ivy_lsp/utils/scope_ranking.py`
- `ivy_lsp/utils/validation.py`
- `ivy_lsp/utils/structural_lint.py` (this one re-exports from infra/ for now, will move to core/ in Phase 2)

Each shim follows the pattern:
```python
"""Backward-compat shim."""
from ivy_lsp.infra.utils.<module_name> import *  # noqa: F401,F403
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline.

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/utils/ ivy_lsp/infra/utils/
git commit -m "refactor: move utils/ -> infra/utils/ (with shims)"
```

### Task 1.5: Rewrite infra/ internal imports

Now that all infra/ files are in place, update imports within `ivy_lsp/infra/` itself so that infra files import from each other using `ivy_lsp.infra.*` paths (not old paths). The infra dependency rule is `infra/ -> nothing` (no core/, lsp/, mcp/ imports).

**Files:**
- Modify: `ivy_lsp/infra/config.py`, `ivy_lsp/infra/observability/*.py`, `ivy_lsp/infra/utils/*.py`

- [ ] **Step 1: Check infra/ files for cross-imports to non-infra modules**

```bash
grep -rn "from ivy_lsp\." ivy_lsp/infra/ --include="*.py" | grep -v "from ivy_lsp\.infra\." | grep -v "__pycache__"
```

Note: `infra/observability/handlers.py` imports `lsprotocol` and TYPE_CHECKING imports from `ivy_lsp.server` — these are acceptable (lsprotocol is a data enum, TYPE_CHECKING is not a runtime dep). The `LspLogHandler` class will be extracted to `lsp/` in Phase 3 (design decision #11).

- [ ] **Step 2: Update internal imports within infra/ that reference old paths**

For any `from ivy_lsp.config import ...` inside infra/, change to `from ivy_lsp.infra.config import ...`.
For any `from ivy_lsp.observability import ...` inside infra/, change to `from ivy_lsp.infra.observability import ...`.
For any `from ivy_lsp.utils import ...` inside infra/, change to `from ivy_lsp.infra.utils import ...`.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline.

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/infra/
git commit -m "refactor: rewrite infra/ internal imports to new paths"
```

### Task 1.6: Rewrite consumer imports to use infra/ paths

Update all files outside infra/ to import from `ivy_lsp.infra.*` instead of old `ivy_lsp.config`, `ivy_lsp.observability`, `ivy_lsp.utils` paths. This is the bulk of the ~550 import changes.

**Files:**
- Modify: All non-infra `ivy_lsp/**/*.py` files
- Modify: All `tests/**/*.py` files

- [ ] **Step 1: Update source files (ivy_lsp/) — batch replace config imports**

Replace all `from ivy_lsp.config import` with `from ivy_lsp.infra.config import` across `ivy_lsp/` (excluding `ivy_lsp/infra/` and shim files).

- [ ] **Step 2: Update source files — batch replace observability imports**

Replace all `from ivy_lsp.observability` with `from ivy_lsp.infra.observability` across `ivy_lsp/` (excluding `ivy_lsp/infra/` and shim files).

- [ ] **Step 3: Update source files — batch replace utils imports**

Replace all `from ivy_lsp.utils` with `from ivy_lsp.infra.utils` across `ivy_lsp/` (excluding `ivy_lsp/infra/` and shim files).

- [ ] **Step 4: Update test files — batch replace all three import families**

Same replacements across `tests/` directory.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline.

- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/ tests/
git commit -m "refactor: rewrite all imports to use infra/ paths"
```

### Task 1.7: Remove Phase 1 shims

**Files:**
- Delete: `ivy_lsp/config.py` (shim), `ivy_lsp/observability/` (shim dir), `ivy_lsp/utils/` (shim dir)

- [ ] **Step 1: Delete shim files**

```bash
rm ivy_lsp/config.py
rm -rf ivy_lsp/observability/
rm -rf ivy_lsp/utils/
```

- [ ] **Step 2: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count. If any test fails, a consumer import was missed in Task 1.6. Fix it before proceeding.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove Phase 1 backward-compat shims"
```

---

## Phase 2: Move core/ (parsing, semantic, analysis, indexer, compilation, diagnostics, workspace, rfc, adapters)

**Risk:** Medium | **Estimated import changes:** ~900

This phase moves all shared domain logic into `core/`. These modules are consumed by both LSP and MCP code. `lsprotocol` enum usage (SymbolKind, DiagnosticSeverity) is acceptable in core/ per design decision #1.

### Task 2.1: Create core/ package skeleton

**Files:**
- Create: `ivy_lsp/core/__init__.py`

- [ ] **Step 1: Write a test that imports from the new core package**

```python
# tests/test_core_skeleton.py
def test_core_package_importable():
    """Verify the core/ package exists and is importable."""
    import ivy_lsp.core
    assert hasattr(ivy_lsp.core, '__name__')
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_core_skeleton.py -x -v
```
Expected: FAIL

- [ ] **Step 3: Create the core package**

```python
# ivy_lsp/core/__init__.py
"""Shared domain logic for ivy-lsp.

May use lsprotocol enums (SymbolKind, DiagnosticSeverity).
Imports allowed: ivy_lsp.infra only.
"""
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_core_skeleton.py -x -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/core/__init__.py tests/test_core_skeleton.py
git commit -m "feat: create core/ package skeleton"
```

### Task 2.2: Move parsing/ -> core/parsing/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/parsing/` -> `ivy_lsp/core/parsing/`

- [ ] **Step 1: Move using git mv**

```bash
git mv ivy_lsp/parsing ivy_lsp/core/parsing
```

- [ ] **Step 2: Add shim at old path**

Create `ivy_lsp/parsing/__init__.py` and per-module shims for all directly-imported submodules:

```python
# ivy_lsp/parsing/__init__.py
"""Backward-compat shim — will be removed in Phase 6."""
from ivy_lsp.core.parsing import *  # noqa: F401,F403
```

Shim files needed (one per directly-imported submodule):
- `ivy_lsp/parsing/symbols.py`
- `ivy_lsp/parsing/ast_to_symbols.py`
- `ivy_lsp/parsing/symbol_to_model.py`
- `ivy_lsp/parsing/tiered_extractor.py`
- `ivy_lsp/parsing/parser_session.py`
- `ivy_lsp/parsing/token_stream.py`
- `ivy_lsp/parsing/fallback_scanner.py`
- `ivy_lsp/parsing/fallback_parser.py`

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/parsing/ ivy_lsp/core/parsing/
git commit -m "refactor: move parsing/ -> core/parsing/ (with shims)"
```

### Task 2.3: Move semantic/ -> core/semantic/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/semantic/` -> `ivy_lsp/core/semantic/`

Follow the same pattern as Task 2.2:
- [ ] **Step 1: git mv**
- [ ] **Step 2: Create shims** (for model.py, nodes.py, edges.py, model_builder.py, analysis_pipeline.py, rfc_annotations.py, snapshots.py)
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git mv ivy_lsp/semantic ivy_lsp/core/semantic
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/semantic/ ivy_lsp/core/semantic/
git commit -m "refactor: move semantic/ -> core/semantic/ (with shims)"
```

### Task 2.4: Move analysis/ -> core/analysis/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/analysis/` -> `ivy_lsp/core/analysis/`

Follow the same pattern as Task 2.2:
- [ ] **Step 1: git mv**
- [ ] **Step 2: Create shims** (for requirement_extractor.py, requirement_graph.py, light_mode_extractor.py, lexer_requirement_extractor.py, formula_analyzer.py, impl_block_parser.py, test_scope.py, pattern_library.py)
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git mv ivy_lsp/analysis ivy_lsp/core/analysis
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/analysis/ ivy_lsp/core/analysis/
git commit -m "refactor: move analysis/ -> core/analysis/ (with shims)"
```

### Task 2.5: Move indexer/ -> core/indexer/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/indexer/` -> `ivy_lsp/core/indexer/`

Follow the same pattern:
- [ ] **Step 1: git mv**
- [ ] **Step 2: Create shims** (for workspace_indexer.py, file_cache.py, include_resolver.py, deep_indexer.py, parallel_indexer.py, scope_manager.py)
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git mv ivy_lsp/indexer ivy_lsp/core/indexer
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/indexer/ ivy_lsp/core/indexer/
git commit -m "refactor: move indexer/ -> core/indexer/ (with shims)"
```

### Task 2.6: Move compilation/ -> core/compilation/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/compilation/` -> `ivy_lsp/core/compilation/`

- [ ] **Step 1-4: Same pattern**

```bash
git mv ivy_lsp/compilation ivy_lsp/core/compilation
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/compilation/ ivy_lsp/core/compilation/
git commit -m "refactor: move compilation/ -> core/compilation/ (with shims)"
```

### Task 2.7: Move diagnostics/ -> core/diagnostics/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/diagnostics/` -> `ivy_lsp/core/diagnostics/`

- [ ] **Step 1-4: Same pattern**

```bash
git mv ivy_lsp/diagnostics ivy_lsp/core/diagnostics
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/diagnostics/ ivy_lsp/core/diagnostics/
git commit -m "refactor: move diagnostics/ -> core/diagnostics/ (with shims)"
```

### Task 2.8: Move workspace/ -> core/workspace/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/workspace/` -> `ivy_lsp/core/workspace/`

- [ ] **Step 1-4: Same pattern**

```bash
git mv ivy_lsp/workspace ivy_lsp/core/workspace
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/workspace/ ivy_lsp/core/workspace/
git commit -m "refactor: move workspace/ -> core/workspace/ (with shims)"
```

### Task 2.9: Move rfc/ -> core/rfc/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/rfc/` -> `ivy_lsp/core/rfc/`

- [ ] **Step 1-4: Same pattern**

```bash
git mv ivy_lsp/rfc ivy_lsp/core/rfc
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/rfc/ ivy_lsp/core/rfc/
git commit -m "refactor: move rfc/ -> core/rfc/ (with shims)"
```

### Task 2.10: Move adapters/ -> core/adapters/ (pure move + shim)

**Files:**
- Move: `ivy_lsp/adapters/` -> `ivy_lsp/core/adapters/`

- [ ] **Step 1-4: Same pattern**

```bash
git mv ivy_lsp/adapters ivy_lsp/core/adapters
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/adapters/ ivy_lsp/core/adapters/
git commit -m "refactor: move adapters/ -> core/adapters/ (with shims)"
```

### Task 2.11: Move standalone core files

**Files:**
- Move: `ivy_lsp/protocols.py` -> `ivy_lsp/core/protocols.py`
- Move: `ivy_lsp/verification.py` -> `ivy_lsp/core/verification.py`
- Copy+move: `ivy_lsp/infra/utils/structural_lint.py` -> `ivy_lsp/core/structural_lint.py`

- [ ] **Step 1: Move protocols.py and verification.py**

```bash
git mv ivy_lsp/protocols.py ivy_lsp/core/protocols.py
git mv ivy_lsp/verification.py ivy_lsp/core/verification.py
```

- [ ] **Step 2: Move structural_lint.py from infra/utils/ to core/**

```bash
git mv ivy_lsp/infra/utils/structural_lint.py ivy_lsp/core/structural_lint.py
```

Update the shim at the old utils path if one exists to point to `ivy_lsp.core.structural_lint`.

- [ ] **Step 3: Create shims at old paths**

```python
# ivy_lsp/protocols.py
"""Backward-compat shim."""
from ivy_lsp.core.protocols import *  # noqa: F401,F403

# ivy_lsp/verification.py
"""Backward-compat shim."""
from ivy_lsp.core.verification import *  # noqa: F401,F403
```

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: move protocols.py, verification.py, structural_lint.py to core/"
```

### Task 2.12: Move features/coverage_hints.py -> core/coverage_hints.py

**Files:**
- Move: `ivy_lsp/features/coverage_hints.py` -> `ivy_lsp/core/coverage_hints.py`

Per design decision #6: zero LSP imports, consumed by MCP tools.

- [ ] **Step 1: Move**

```bash
git mv ivy_lsp/features/coverage_hints.py ivy_lsp/core/coverage_hints.py
```

- [ ] **Step 2: Create shim**

```python
# ivy_lsp/features/coverage_hints.py
"""Backward-compat shim."""
from ivy_lsp.core.coverage_hints import *  # noqa: F401,F403
```

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/features/coverage_hints.py ivy_lsp/core/coverage_hints.py
git commit -m "refactor: move coverage_hints.py -> core/ (zero LSP deps)"
```

### Task 2.13: Move features/patterns.py -> core/analysis/

**Files:**
- Move: `ivy_lsp/features/patterns.py` -> `ivy_lsp/core/analysis/patterns.py`

Per design decision #5: zero LSP imports, consumed by MCP tools only. Note: `analysis/pattern_library.py` already exists in the codebase as a separate module. `features/patterns.py` is a different file with different content -- it moves alongside `pattern_library.py`, not as a replacement.

- [ ] **Step 1: Verify both files exist and are distinct**

```bash
head -5 ivy_lsp/core/analysis/pattern_library.py
head -5 ivy_lsp/features/patterns.py
```

- [ ] **Step 2: Move features/patterns.py to core/analysis/patterns.py**

```bash
git mv ivy_lsp/features/patterns.py ivy_lsp/core/analysis/patterns.py
```

- [ ] **Step 3: Create shim at old path**

```python
# ivy_lsp/features/patterns.py
"""Backward-compat shim."""
from ivy_lsp.core.analysis.patterns import *  # noqa: F401,F403
```

- [ ] **Step 4: Run full test suite and commit**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add ivy_lsp/features/patterns.py ivy_lsp/core/analysis/patterns.py
git commit -m "refactor: move features/patterns.py -> core/analysis/patterns.py"
```

### Task 2.14: Rewrite core/ internal imports

**Files:**
- Modify: All `ivy_lsp/core/**/*.py` files

Update all imports within `core/` to use `ivy_lsp.core.*` paths instead of old paths. Also ensure `core/` files import infra modules via `ivy_lsp.infra.*`.

- [ ] **Step 1: Find all old-style imports in core/**

```bash
grep -rn "from ivy_lsp\.\(parsing\|semantic\|analysis\|indexer\|compilation\|diagnostics\|workspace\|rfc\|adapters\)\." ivy_lsp/core/ --include="*.py" | grep -v "from ivy_lsp\.core\." | head -40
```

- [ ] **Step 2: Batch-replace imports**

Replace patterns:
- `from ivy_lsp.parsing.` -> `from ivy_lsp.core.parsing.`
- `from ivy_lsp.semantic.` -> `from ivy_lsp.core.semantic.`
- `from ivy_lsp.analysis.` -> `from ivy_lsp.core.analysis.`
- `from ivy_lsp.indexer.` -> `from ivy_lsp.core.indexer.`
- `from ivy_lsp.compilation.` -> `from ivy_lsp.core.compilation.`
- `from ivy_lsp.diagnostics.` -> `from ivy_lsp.core.diagnostics.`
- `from ivy_lsp.workspace.` -> `from ivy_lsp.core.workspace.`
- `from ivy_lsp.rfc.` -> `from ivy_lsp.core.rfc.`
- `from ivy_lsp.adapters.` -> `from ivy_lsp.core.adapters.`

Also update any remaining `from ivy_lsp.config` -> `from ivy_lsp.infra.config`, etc.

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/core/
git commit -m "refactor: rewrite core/ internal imports to new paths"
```

### Task 2.15: Rewrite consumer imports to use core/ paths

**Files:**
- Modify: All non-core, non-infra `ivy_lsp/**/*.py` files
- Modify: All `tests/**/*.py` files

- [ ] **Step 1: Update remaining source files (ivy_lsp/) — batch replace all 9 families**

Replace all `from ivy_lsp.{parsing,semantic,analysis,...}` with `from ivy_lsp.core.{...}` across ivy_lsp/ (excluding core/, infra/, and shim files).

- [ ] **Step 2: Update test files — batch replace**

Same replacements across tests/.

- [ ] **Step 3: Update @patch() paths in tests**

Using the inventory from Task 0.2, update patch targets:
- `ivy_lsp.parsing.ast_to_symbols.ast_to_symbols` -> `ivy_lsp.core.parsing.ast_to_symbols.ast_to_symbols`
- `ivy_lsp.parsing.fallback_scanner.fallback_scan` -> `ivy_lsp.core.parsing.fallback_scanner.fallback_scan`

- [ ] **Step 4: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/ tests/
git commit -m "refactor: rewrite all imports to use core/ paths"
```

### Task 2.16: Remove Phase 2 shims

**Files:**
- Delete: All Phase 2 shim directories/files (parsing/, semantic/, analysis/, etc. at old locations)

- [ ] **Step 1: Delete shim directories**

```bash
rm -rf ivy_lsp/parsing/ ivy_lsp/semantic/ ivy_lsp/analysis/ ivy_lsp/indexer/
rm -rf ivy_lsp/compilation/ ivy_lsp/diagnostics/ ivy_lsp/workspace/ ivy_lsp/rfc/ ivy_lsp/adapters/
rm -f ivy_lsp/protocols.py ivy_lsp/verification.py
```

Note: `ivy_lsp/features/coverage_hints.py` and `ivy_lsp/features/patterns.py` shims remain until Phase 3 (when features/ itself moves).

- [ ] **Step 2: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove Phase 2 backward-compat shims"
```

---

## Phase 3: Move lsp/ (server, features -> flat + sub-packages)

**Risk:** Medium-High | **Estimated import changes:** ~500

This phase creates the LSP protocol shell. The `features/` directory is dissolved: navigation files go to `lsp/navigation/`, diagnostic files to `lsp/diagnostics/`, UI files to `lsp/ui/`, and remaining feature files become flat `lsp/` files.

### Task 3.1: Create lsp/ package skeleton with sub-packages

**Files:**
- Create: `ivy_lsp/lsp/__init__.py`, `ivy_lsp/lsp/navigation/__init__.py`, `ivy_lsp/lsp/diagnostics/__init__.py`, `ivy_lsp/lsp/ui/__init__.py`

- [ ] **Step 1: Write skeleton test**

```python
# tests/test_lsp_skeleton.py
def test_lsp_package_importable():
    import ivy_lsp.lsp
    import ivy_lsp.lsp.navigation
    import ivy_lsp.lsp.diagnostics
    import ivy_lsp.lsp.ui
```

- [ ] **Step 2: Run test — expect FAIL**
- [ ] **Step 3: Create packages**

```python
# ivy_lsp/lsp/__init__.py
"""LSP protocol shell for ivy-lsp."""

# ivy_lsp/lsp/navigation/__init__.py
"""LSP navigation features (definition, references, hover, etc.)."""

# ivy_lsp/lsp/diagnostics/__init__.py
"""LSP diagnostic publishing and computation."""

# ivy_lsp/lsp/ui/__init__.py
"""LSP UI features (code lens, folding, selection, monitoring, status)."""
```

- [ ] **Step 4: Run test — expect PASS**
- [ ] **Step 5: Commit**

```bash
git add ivy_lsp/lsp/ tests/test_lsp_skeleton.py
git commit -m "feat: create lsp/ package skeleton with sub-packages"
```

### Task 3.2: Move server files to lsp/

**Files:**
- Move: `ivy_lsp/server.py` -> `ivy_lsp/lsp/server.py`
- Move: `ivy_lsp/server_setup.py` -> `ivy_lsp/lsp/server_setup.py`
- Move: `ivy_lsp/bulk_orchestrator.py` -> `ivy_lsp/lsp/bulk_orchestrator.py`
- Move: `ivy_lsp/index_builder.py` -> `ivy_lsp/lsp/index_builder.py`
- Move: `ivy_lsp/pygls_patches.py` -> `ivy_lsp/lsp/pygls_patches.py`

- [ ] **Step 1: Move files**

```bash
git mv ivy_lsp/server.py ivy_lsp/lsp/server.py
git mv ivy_lsp/server_setup.py ivy_lsp/lsp/server_setup.py
git mv ivy_lsp/bulk_orchestrator.py ivy_lsp/lsp/bulk_orchestrator.py
git mv ivy_lsp/index_builder.py ivy_lsp/lsp/index_builder.py
git mv ivy_lsp/pygls_patches.py ivy_lsp/lsp/pygls_patches.py
```

- [ ] **Step 2: Create shims at old paths**

Each shim re-exports everything from the new location.

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move server files to lsp/ (with shims)"
```

### Task 3.3: Extract LspLogHandler from observability/handlers.py to lsp/lsp_log_handler.py

**Files:**
- Create: `ivy_lsp/lsp/lsp_log_handler.py`
- Modify: `ivy_lsp/infra/observability/handlers.py` (remove LspLogHandler class)

Per design decision #11: split `observability/handlers.py` — `LspLogHandler` goes to `lsp/`, non-LSP handlers stay in `infra/observability/`.

- [ ] **Step 1: Write test for the extracted module**

```python
# tests/test_lsp_log_handler_location.py
def test_lsp_log_handler_importable_from_lsp():
    from ivy_lsp.lsp.lsp_log_handler import LspLogHandler
    assert LspLogHandler is not None
```

- [ ] **Step 2: Run test — expect FAIL**
- [ ] **Step 3: Copy LspLogHandler class to lsp/lsp_log_handler.py**

Copy the entire `LspLogHandler` class (lines 28-~130 of `infra/observability/handlers.py`) to the new file, with its imports.

- [ ] **Step 4: Remove LspLogHandler from infra/observability/handlers.py**

Delete the `LspLogHandler` class entirely from `infra/observability/handlers.py`. Do NOT add a re-import from `ivy_lsp.lsp.lsp_log_handler` — that would violate the dependency rule (`infra/` must not import from `lsp/`). Consumers that imported `LspLogHandler` from `ivy_lsp.observability.handlers` or `ivy_lsp.infra.observability.handlers` must be updated to import from `ivy_lsp.lsp.lsp_log_handler` directly.

Update any `__init__.py` re-exports in `infra/observability/` that previously exported `LspLogHandler` — remove those entries.

- [ ] **Step 5: Run full test suite**
- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/lsp/lsp_log_handler.py ivy_lsp/infra/observability/handlers.py
git commit -m "refactor: extract LspLogHandler to lsp/lsp_log_handler.py"
```

### Task 3.4: Move navigation features to lsp/navigation/

**Files:**
- Move: `ivy_lsp/features/definition.py` -> `ivy_lsp/lsp/navigation/definition.py`
- Move: `ivy_lsp/features/implementation.py` -> `ivy_lsp/lsp/navigation/implementation.py`
- Move: `ivy_lsp/features/references.py` -> `ivy_lsp/lsp/navigation/references.py`
- Move: `ivy_lsp/features/call_hierarchy.py` -> `ivy_lsp/lsp/navigation/call_hierarchy.py`
- Move: `ivy_lsp/features/hover.py` -> `ivy_lsp/lsp/navigation/hover.py`

- [ ] **Step 1: Move files**

```bash
git mv ivy_lsp/features/definition.py ivy_lsp/lsp/navigation/definition.py
git mv ivy_lsp/features/implementation.py ivy_lsp/lsp/navigation/implementation.py
git mv ivy_lsp/features/references.py ivy_lsp/lsp/navigation/references.py
git mv ivy_lsp/features/call_hierarchy.py ivy_lsp/lsp/navigation/call_hierarchy.py
git mv ivy_lsp/features/hover.py ivy_lsp/lsp/navigation/hover.py
```

- [ ] **Step 2: Create shims at old paths**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move navigation features to lsp/navigation/"
```

### Task 3.5: Move diagnostic features to lsp/diagnostics/ (with renames)

**Files:**
- Move+Rename: `ivy_lsp/features/diagnostics.py` -> `ivy_lsp/lsp/diagnostics/publisher.py`
- Move+Rename: `ivy_lsp/features/diagnostic_compute.py` -> `ivy_lsp/lsp/diagnostics/compute.py`

Per design decision #9: rename `diagnostics.py` -> `publisher.py`, `diagnostic_compute.py` -> `compute.py`.

- [ ] **Step 1: Move and rename**

```bash
git mv ivy_lsp/features/diagnostics.py ivy_lsp/lsp/diagnostics/publisher.py
git mv ivy_lsp/features/diagnostic_compute.py ivy_lsp/lsp/diagnostics/compute.py
```

- [ ] **Step 2: Create shims at old paths (using old names pointing to new names)**

```python
# ivy_lsp/features/diagnostics.py
"""Backward-compat shim."""
from ivy_lsp.lsp.diagnostics.publisher import *  # noqa: F401,F403

# ivy_lsp/features/diagnostic_compute.py
"""Backward-compat shim."""
from ivy_lsp.lsp.diagnostics.compute import *  # noqa: F401,F403
```

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move diagnostic features to lsp/diagnostics/ (publisher, compute)"
```

### Task 3.6: Move UI features to lsp/ui/

**Files:**
- Move: `ivy_lsp/features/code_lens.py` -> `ivy_lsp/lsp/ui/code_lens.py`
- Move: `ivy_lsp/features/folding_range.py` -> `ivy_lsp/lsp/ui/folding_range.py`
- Move: `ivy_lsp/features/selection_range.py` -> `ivy_lsp/lsp/ui/selection_range.py`
- Move: `ivy_lsp/features/monitoring.py` -> `ivy_lsp/lsp/ui/monitoring.py`
- Move: `ivy_lsp/features/status.py` -> `ivy_lsp/lsp/ui/status.py`

- [ ] **Step 1: Move files**
- [ ] **Step 2: Create shims**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git mv ivy_lsp/features/code_lens.py ivy_lsp/lsp/ui/code_lens.py
git mv ivy_lsp/features/folding_range.py ivy_lsp/lsp/ui/folding_range.py
git mv ivy_lsp/features/selection_range.py ivy_lsp/lsp/ui/selection_range.py
git mv ivy_lsp/features/monitoring.py ivy_lsp/lsp/ui/monitoring.py
git mv ivy_lsp/features/status.py ivy_lsp/lsp/ui/status.py
# Create shims...
pytest tests/ -x --tb=short -q 2>&1 | tail -5
git add -A
git commit -m "refactor: move UI features to lsp/ui/"
```

### Task 3.7: Move remaining flat features to lsp/

**Files:**
- Move: `ivy_lsp/features/completion.py` -> `ivy_lsp/lsp/completion.py`
- Move: `ivy_lsp/features/rename.py` -> `ivy_lsp/lsp/rename.py`
- Move: `ivy_lsp/features/code_action.py` -> `ivy_lsp/lsp/code_action.py`
- Move: `ivy_lsp/features/signature_help.py` -> `ivy_lsp/lsp/signature_help.py`
- Move: `ivy_lsp/features/document_symbols.py` -> `ivy_lsp/lsp/document_symbols.py`
- Move: `ivy_lsp/features/workspace_symbols.py` -> `ivy_lsp/lsp/workspace_symbols.py`
- Move: `ivy_lsp/features/document_highlight.py` -> `ivy_lsp/lsp/document_highlight.py`
- Move: `ivy_lsp/features/commands.py` -> `ivy_lsp/lsp/commands.py`
- Move: `ivy_lsp/features/commands_extended.py` -> `ivy_lsp/lsp/commands_extended.py`
- Move: `ivy_lsp/features/visualization.py` -> `ivy_lsp/lsp/visualization.py`
- Move: `ivy_lsp/features/viz_coverage.py` -> `ivy_lsp/lsp/viz_coverage.py`
- Move: `ivy_lsp/features/viz_graphs.py` -> `ivy_lsp/lsp/viz_graphs.py`
- Move: `ivy_lsp/features/viz_suggestions.py` -> `ivy_lsp/lsp/viz_suggestions.py`

- [ ] **Step 1: Move all flat feature files**

```bash
for f in completion rename code_action signature_help document_symbols workspace_symbols document_highlight commands commands_extended visualization viz_coverage viz_graphs viz_suggestions; do
  git mv ivy_lsp/features/${f}.py ivy_lsp/lsp/${f}.py
done
```

- [ ] **Step 2: Create shims for each at old paths**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move flat feature files to lsp/"
```

### Task 3.8: Rewrite lsp/ internal imports

**Files:**
- Modify: All `ivy_lsp/lsp/**/*.py` files

- [ ] **Step 1: Update all imports within lsp/ to use new paths**

Replace:
- `from ivy_lsp.features.` -> appropriate `from ivy_lsp.lsp.` path
- `from ivy_lsp.server` -> `from ivy_lsp.lsp.server`
- `from ivy_lsp.server_setup` -> `from ivy_lsp.lsp.server_setup`
- etc.

- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit**

```bash
git add ivy_lsp/lsp/
git commit -m "refactor: rewrite lsp/ internal imports to new paths"
```

### Task 3.9: Rewrite consumer imports to use lsp/ paths

**Files:**
- Modify: `ivy_lsp/__main__.py`, `ivy_lsp/mcp_server.py`, all `tests/**/*.py`

- [ ] **Step 1: Update __main__.py**

```python
# Change:
from ivy_lsp.server import IvyLanguageServer
# To:
from ivy_lsp.lsp.server import IvyLanguageServer
```

And similar for all other imports in __main__.py.

- [ ] **Step 2: Update test files**

Update imports and @patch() paths per the inventory.

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add ivy_lsp/__main__.py tests/
git commit -m "refactor: rewrite all imports to use lsp/ paths"
```

### Task 3.10: Remove Phase 3 shims and delete features/

**Files:**
- Delete: `ivy_lsp/features/` (all remaining shims), `ivy_lsp/server.py` (shim), `ivy_lsp/server_setup.py` (shim), `ivy_lsp/bulk_orchestrator.py` (shim), `ivy_lsp/index_builder.py` (shim), `ivy_lsp/pygls_patches.py` (shim)

- [ ] **Step 1: Delete all shim files and empty features/ directory**

```bash
rm -rf ivy_lsp/features/
rm -f ivy_lsp/server.py ivy_lsp/server_setup.py ivy_lsp/bulk_orchestrator.py
rm -f ivy_lsp/index_builder.py ivy_lsp/pygls_patches.py
```

- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove Phase 3 shims and delete features/"
```

---

## Phase 4: Move mcp/ (tools, formatters, bridge, sidecar)

**Risk:** Medium | **Estimated import changes:** ~200

This phase moves MCP-related files into the `mcp/` package. The mcp_server.py monolith is NOT split yet (that's Phase 5).

### Task 4.1: Create mcp/ package skeleton

**Files:**
- Create: `ivy_lsp/mcp/__init__.py`

- [ ] **Step 1: Write skeleton test**
- [ ] **Step 2: Create package**

```python
# ivy_lsp/mcp/__init__.py
"""MCP protocol shell for ivy-lsp."""
```

- [ ] **Step 3: Run test, commit**

### Task 4.2: Move tools/ -> mcp/tools/ (pure move)

**Files:**
- Move: `ivy_lsp/tools/` -> `ivy_lsp/mcp/tools/`

- [ ] **Step 1: Move**

```bash
git mv ivy_lsp/tools ivy_lsp/mcp/tools
```

- [ ] **Step 2: Create shim at old path**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move tools/ -> mcp/tools/ (with shim)"
```

### Task 4.3: Rename formatters/_primitives.py -> primitives.py

**Files:**
- Rename: `ivy_lsp/mcp/tools/formatters/_primitives.py` -> `ivy_lsp/mcp/tools/formatters/primitives.py`

Per design decision #9.

- [ ] **Step 1: Rename**

```bash
git mv ivy_lsp/mcp/tools/formatters/_primitives.py ivy_lsp/mcp/tools/formatters/primitives.py
```

- [ ] **Step 2: Update imports in formatters/ files**

Change `from ._primitives import` to `from .primitives import` in all formatter files.

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

### Task 4.4: Move MCP bridge files to mcp/

**Files:**
- Move: `ivy_lsp/mcp_bridge.py` -> `ivy_lsp/mcp/bridge.py`
- Move: `ivy_lsp/mcp_sidecar.py` -> `ivy_lsp/mcp/sidecar.py`
- Move: `ivy_lsp/sidecar_client.py` -> `ivy_lsp/mcp/client.py`

- [ ] **Step 1: Move files**

```bash
git mv ivy_lsp/mcp_bridge.py ivy_lsp/mcp/bridge.py
git mv ivy_lsp/mcp_sidecar.py ivy_lsp/mcp/sidecar.py
git mv ivy_lsp/sidecar_client.py ivy_lsp/mcp/client.py
```

- [ ] **Step 2: Create shims at old paths**
- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move MCP bridge/sidecar/client to mcp/"
```

### Task 4.5: Move mcp_server.py -> mcp/server.py (pure move, no split yet)

**Files:**
- Move: `ivy_lsp/mcp_server.py` -> `ivy_lsp/mcp/server.py`

- [ ] **Step 1: Move**

```bash
git mv ivy_lsp/mcp_server.py ivy_lsp/mcp/server.py
```

- [ ] **Step 2: Create shim at old path**

```python
# ivy_lsp/mcp_server.py
"""Backward-compat shim."""
from ivy_lsp.mcp.server import *  # noqa: F401,F403
```

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: move mcp_server.py -> mcp/server.py (with shim)"
```

### Task 4.6: Rewrite mcp/ internal imports and consumer imports

**Files:**
- Modify: All `ivy_lsp/mcp/**/*.py` files
- Modify: `ivy_lsp/__main__.py`, `tests/**/*.py`

- [ ] **Step 1: Update mcp/ internal imports**

Replace:
- `from ivy_lsp.tools.` -> `from ivy_lsp.mcp.tools.`
- `from ivy_lsp.mcp_server` -> `from ivy_lsp.mcp.server`
- `from ivy_lsp.sidecar_client` -> `from ivy_lsp.mcp.client`
- `from ivy_lsp.mcp_bridge` -> `from ivy_lsp.mcp.bridge`
- `from ivy_lsp.mcp_sidecar` -> `from ivy_lsp.mcp.sidecar`

- [ ] **Step 2: Update __main__.py and test imports**

Update @patch() paths per inventory:
- `ivy_lsp.mcp_server.sidecar_client` -> `ivy_lsp.mcp.server.sidecar_client`
- `ivy_lsp.mcp_server.shared_ivy_check` -> `ivy_lsp.mcp.server.shared_ivy_check`
- `ivy_lsp.sidecar_client._fetch_health` -> `ivy_lsp.mcp.client._fetch_health`

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: rewrite all imports to use mcp/ paths"
```

### Task 4.7: Remove Phase 4 shims

**Files:**
- Delete: `ivy_lsp/tools/` (shim), `ivy_lsp/mcp_server.py` (shim), `ivy_lsp/mcp_bridge.py` (shim), `ivy_lsp/mcp_sidecar.py` (shim), `ivy_lsp/sidecar_client.py` (shim)

- [ ] **Step 1: Delete shims**

```bash
rm -rf ivy_lsp/tools/
rm -f ivy_lsp/mcp_server.py ivy_lsp/mcp_bridge.py ivy_lsp/mcp_sidecar.py ivy_lsp/sidecar_client.py
```

- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove Phase 4 shims"
```

---

## Phase 5: Refactor + split mcp_server.py (closure -> class, then extract)

**Risk:** HIGHEST | **Estimated import changes:** ~100

This phase converts the 15+ nonlocal closures in `start_mcp()` into a `McpServerState` class, then extracts `ToolContext` into `mcp/context.py`.

### Task 5.1: Extract ToolContext to mcp/context.py

**Files:**
- Create: `ivy_lsp/mcp/context.py`
- Modify: `ivy_lsp/mcp/server.py`

The `ToolContext` dataclass is already self-contained. This is a straightforward extraction.

- [ ] **Step 1: Write test for new location**

```python
# tests/test_mcp_context_location.py
def test_tool_context_importable_from_mcp_context():
    from ivy_lsp.mcp.context import ToolContext
    assert ToolContext is not None
```

- [ ] **Step 2: Run test — expect FAIL**

- [ ] **Step 3: Move ToolContext class and helpers to mcp/context.py**

Cut the `ToolContext` dataclass (lines 76-272 of mcp/server.py), the `_validate_path` function, the `_check_structural_issues` function, and their imports into `ivy_lsp/mcp/context.py`.

```python
# ivy_lsp/mcp/context.py
"""Shared context passed to every MCP tool registration module."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

# ... (ToolContext class, _validate_path, _check_structural_issues)
```

- [ ] **Step 4: Update mcp/server.py to import from mcp/context.py**

```python
from ivy_lsp.mcp.context import ToolContext, _validate_path, _check_structural_issues
```

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add ivy_lsp/mcp/context.py ivy_lsp/mcp/server.py tests/test_mcp_context_location.py
git commit -m "refactor: extract ToolContext to mcp/context.py"
```

### Task 5.2: Convert start_mcp() closures to McpServerState class

**Files:**
- Modify: `ivy_lsp/mcp/server.py`

This is the highest-risk task. The `start_mcp()` function uses 15+ closures with nonlocal variables. Convert them into methods on a `McpServerState` class.

- [ ] **Step 1: Identify all closure-captured state variables**

Variables that need to become instance attributes:
- `semantic_model`, `_model_lock`, `_model_build_attempted`, `_model_build_error`, `_model_building`
- `_req_graph`, `_req_graph_lock`, `_req_graph_import_failed`, `_req_graph_last_failure`
- `_cached_ivy_files`, `_cached_ivy_files_lock`
- `_basename_cache`, `_basename_cache_lock`
- `root`, `staging_dir`, `_resolver`, `_include_paths`, `_effective_exclude_dirs`
- `_MODEL_RETRY_COOLDOWN`, `_MODEL_BUILD_TIMEOUT`, `_REQ_GRAPH_COOLDOWN`
- `discovered_stdlib`

- [ ] **Step 2: Write a test that validates McpServerState class creation**

```python
# tests/test_mcp_server_state.py
def test_mcp_server_state_init():
    """McpServerState can be instantiated with basic args."""
    from ivy_lsp.mcp.server import McpServerState
    state = McpServerState(root="/tmp/test", staging_dir=None)
    assert state.root == "/tmp/test"
    assert state.semantic_model is None
```

- [ ] **Step 3: Run test — expect FAIL**

- [ ] **Step 4: Create McpServerState class**

Convert all closures to methods. The class should have:

```python
class McpServerState:
    """Mutable state for the MCP server.

    Replaces the 15+ nonlocal closures in start_mcp() with a proper
    class that holds workspace state, lazy builders, and helper methods.
    """

    def __init__(
        self,
        root: str,
        staging_dir: str | None,
        semantic_model: Any = None,
        requirement_graph: Any = None,
        resolver: Any = None,
        include_paths: list[str] | None = None,
        exclude_dirs: frozenset[str] = frozenset(),
        ws_config: Any = None,
    ):
        self.root = root
        self.staging_dir = staging_dir
        self.semantic_model = semantic_model
        # ... all other state variables

    def find_ivy_files(self, search_root: str) -> list[str]: ...
    def find_ivy_files_cached(self, search_root: str) -> list[str]: ...
    def get_basename_cache(self) -> dict[str, list[str]]: ...
    def make_resolve_callback(self) -> Callable: ...
    async def get_model(self) -> Any: ...
    async def get_model_or_none(self, timeout: float = 5.0) -> Any: ...
    def get_model_status(self) -> dict: ...
    async def get_req_graph(self) -> Any: ...
    def build_model(self) -> Any: ...
    def build_requirement_graph(self) -> Any: ...
    async def make_viz_server_proxy(self) -> Any: ...
    def build_tool_context(self) -> ToolContext: ...
```

- [ ] **Step 5: Rewrite start_mcp() to use McpServerState**

```python
def start_mcp(...) -> Any:
    # ... initial setup (unchanged) ...

    state = McpServerState(
        root=root,
        staging_dir=staging_dir,
        semantic_model=semantic_model,
        requirement_graph=requirement_graph,
        resolver=_resolver,
        include_paths=_include_paths,
        exclude_dirs=_effective_exclude_dirs,
        ws_config=ws_config,
    )

    ctx = state.build_tool_context()
    mcp = create_mcp_app(ctx)

    # ... rest of start_mcp (prewarm, sidecar monitor, run) ...
```

- [ ] **Step 6: Run full test suite after EACH method conversion**

Convert methods one at a time, running the test suite after each. **Create intermediate commits** after every 3-4 methods to provide rollback points for this highest-risk task:

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
# After each batch of methods:
git add ivy_lsp/mcp/server.py
git commit -m "refactor: convert start_mcp() closures to McpServerState (batch N)"
```

- [ ] **Step 7: Final commit**

```bash
git add ivy_lsp/mcp/server.py tests/test_mcp_server_state.py
git commit -m "refactor: complete McpServerState conversion — all closures extracted"
```

---

## Phase 6: Entry point, cleanup, full validation

**Risk:** Low | **Estimated import changes:** ~50

### Task 6.1: Update __main__.py entry point

**Files:**
- Modify: `ivy_lsp/__main__.py`

- [ ] **Step 1: Update all imports to final paths**

Replace:
```python
from ivy_lsp.observability import ...  # old
# with:
from ivy_lsp.infra.observability import ...  # new

from ivy_lsp.mcp_server import start_mcp  # old
# with:
from ivy_lsp.mcp.server import start_mcp  # new

from ivy_lsp.server import IvyLanguageServer  # old
# with:
from ivy_lsp.lsp.server import IvyLanguageServer  # new
```

Also update:
- `from ivy_lsp.config import get_config` -> `from ivy_lsp.infra.config import get_config`
- `from ivy_lsp.workspace.detection import detect_ivy_workspace` -> `from ivy_lsp.core.workspace.detection import detect_ivy_workspace`
- `from ivy_lsp.workspace.context import WorkspaceContext` -> `from ivy_lsp.core.workspace.context import WorkspaceContext`
- `from ivy_lsp.index_builder import cli_index` -> `from ivy_lsp.lsp.index_builder import cli_index`

- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit**

```bash
git add ivy_lsp/__main__.py
git commit -m "refactor: update __main__.py to use final import paths"
```

### Task 6.2: Update @patch() paths in remaining tests

**Files:**
- Modify: Test files from the inventory (Task 0.2)

- [ ] **Step 1: Cross-reference patch inventory with current test state**

```bash
grep -rn 'patch("ivy_lsp\.' tests/ --include="*.py" | grep -v "\.core\.\|\.lsp\.\|\.mcp\.\|\.infra\."
```

Any results are stale @patch() paths that need updating.

- [ ] **Step 2: Update remaining @patch() paths**

Using the inventory from Task 0.2, update all remaining paths.

- [ ] **Step 3: Run full test suite**
- [ ] **Step 4: Commit**

```bash
git add tests/
git commit -m "refactor: update remaining @patch() paths to new module locations"
```

### Task 6.3: Remove all remaining shim files and skeleton tests

**Files:**
- Delete: Any remaining backward-compat shim files
- Delete: `tests/test_infra_skeleton.py`, `tests/test_core_skeleton.py`, `tests/test_lsp_skeleton.py`, `tests/test_lsp_log_handler_location.py`, `tests/test_mcp_context_location.py`, `tests/test_mcp_server_state.py`

- [ ] **Step 1: Find and delete any remaining shims**

```bash
grep -rl "Backward-compat shim" ivy_lsp/ --include="*.py"
```

Delete each one found.

- [ ] **Step 2: Delete scaffold tests**

These were verification scaffolding, not permanent tests:
```bash
rm -f tests/test_infra_skeleton.py tests/test_core_skeleton.py tests/test_lsp_skeleton.py
rm -f tests/test_lsp_log_handler_location.py tests/test_mcp_context_location.py tests/test_mcp_server_state.py
```

- [ ] **Step 3: Run full test suite**

```bash
pytest tests/ -x --tb=short -q 2>&1 | tail -5
```
Expected: Same pass count as baseline (minus deleted scaffold tests, plus any new structural tests).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove all backward-compat shims and scaffold tests"
```

### Task 6.4: Validate dependency rules

**Files:**
- Read: All `ivy_lsp/**/*.py`

- [ ] **Step 1: Check infra/ imports nothing from core/, lsp/, mcp/**

```bash
grep -rn "from ivy_lsp\.\(core\|lsp\|mcp\)\." ivy_lsp/infra/ --include="*.py" | grep -v "TYPE_CHECKING" | grep -v "__pycache__"
```
Expected: No output (except TYPE_CHECKING-guarded imports).

- [ ] **Step 2: Check core/ imports only from infra/**

```bash
grep -rn "from ivy_lsp\.\(lsp\|mcp\)\." ivy_lsp/core/ --include="*.py" | grep -v "TYPE_CHECKING" | grep -v "__pycache__"
```
Expected: No output.

- [ ] **Step 3: Check lsp/ and mcp/ don't cross-import**

```bash
# lsp/ should not import from mcp/
grep -rn "from ivy_lsp\.mcp\." ivy_lsp/lsp/ --include="*.py" | grep -v "TYPE_CHECKING"
# mcp/ should not import from lsp/
grep -rn "from ivy_lsp\.lsp\." ivy_lsp/mcp/ --include="*.py" | grep -v "TYPE_CHECKING"
```
Expected: No output.

- [ ] **Step 4: Fix any violations found**
- [ ] **Step 5: Commit fixes if any**

### Task 6.5: Full validation suite

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -x --tb=short -q
```
Expected: ~1,965 tests pass.

- [ ] **Step 2: Run pyright**

```bash
pyright ivy_lsp/
```
Expected: No new errors beyond pre-existing ones.

- [ ] **Step 3: Verify no stale old-path imports remain**

```bash
# Check for any imports using the old flat structure
grep -rn "from ivy_lsp\.\(features\|tools\|observability\|utils\|config\)\." ivy_lsp/ --include="*.py" | \
  grep -v "from ivy_lsp\.\(core\|lsp\|mcp\|infra\)\." | \
  grep -v "__pycache__" | \
  grep -v "shim"
```
Expected: No output.

- [ ] **Step 4: Verify __init__.py re-exports are clean**

```bash
find ivy_lsp -name "__init__.py" | wc -l
```
Expected: ~27 __init__.py files (15 original + ~12 new).

- [ ] **Step 5: LSP smoke test (hover + go-to-definition)**

If a real .ivy workspace is available:
```bash
# Start LSP server in the background, send an initialize request, then a
# textDocument/hover request for a known symbol. Verify a non-error response.
# This can be done via a simple Python script using pygls test client, or
# by briefly running the server and checking startup logs:
timeout 5 python -m ivy_lsp --lsp-only < /dev/null 2>&1 | head -5
```
Expected: No import errors, server starts cleanly.

- [ ] **Step 6: MCP smoke test (tool registration)**

```bash
# Start MCP server in test mode (_return_app=True) and verify tools register:
python -c "
from ivy_lsp.mcp.server import start_mcp
app = start_mcp(workspace_root='/tmp', _return_app=True)
print('MCP app created successfully, tools registered')
" 2>&1 | tail -3
```
Expected: "MCP app created successfully" (or graceful ImportError if mcp extras not installed).

- [ ] **Step 7: Plugin smoke test (/nct-health)**

If the PANTHER plugin infrastructure is available:
```bash
# Verify the ivy-lsp plugin hooks and commands still resolve their imports:
python -c "from ivy_lsp.mcp.context import ToolContext; print('ToolContext importable')"
python -c "from ivy_lsp.lsp.server import IvyLanguageServer; print('IvyLanguageServer importable')"
python -c "from ivy_lsp.core.verification import run_ivy_check; print('run_ivy_check importable')"
```
Expected: All three print statements succeed.

- [ ] **Step 8: Create final commit**

```bash
git add -A
git commit -m "refactor: ivy-lsp 3-layer package redesign complete

Reorganized ~128 source files into:
- core/: shared domain logic (parsing, semantic, analysis, etc.)
- lsp/: LSP protocol shell (server, features, navigation, diagnostics, ui)
- mcp/: MCP protocol shell (server, tools, formatters, bridge, sidecar)
- infra/: cross-cutting infrastructure (config, observability, utils)

Dependency rules enforced:
- infra/ -> nothing
- core/ -> infra/ only
- lsp/, mcp/ -> core/ + infra/
- lsp/ and mcp/ never cross-import

No functionality changes. All ~1,965 tests pass."
```

---

## Summary

| Phase | Tasks | Key Risk | Commits |
|-------|-------|----------|---------|
| 0 | 2 | None | 2 (tag + inventory) |
| 1 | 7 | Low (~550 import changes) | 7 |
| 2 | 16 | Medium (~900 import changes) | ~12 |
| 3 | 10 | Medium-High (~500 import changes) | ~8 |
| 4 | 7 | Medium (~200 import changes) | ~6 |
| 5 | 2 | **Highest** (closure -> class) | 2 |
| 6 | 5 | Low (~50 import changes) | ~4 |
| **Total** | **49 tasks** | | **~41 commits** |

### Strategy Notes

1. **Shim pattern**: Each move creates a backward-compat shim at the old path. Shims are removed at the end of each phase after all consumers are updated. This ensures tests pass after every commit.

2. **Separate move vs content commits**: `git mv` preserves blame. Import rewrites are separate commits. This keeps the git log clean and reviewable.

3. **Test after every commit**: The `pytest tests/ -x --tb=short -q` command is run after every step. If it fails, stop and fix before proceeding.

4. **@patch() inventory**: The highest-risk items are `@patch()` string paths in tests. The Phase 0 inventory tracks them all; they are updated in the same commit as the corresponding move.
