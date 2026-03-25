# ivy-lsp Package Boundary Redesign

**Date:** 2026-03-25
**Status:** Approved
**Scope:** ivy-lsp submodule (~40K LOC, 116 files)

## Context

The ivy-lsp submodule has grown organically with LSP and MCP protocol code mixed alongside domain logic. The current flat structure makes it hard to reason about boundaries: `features/` contains both LSP protocol handlers and reusable domain logic, `mcp_server.py` is a 1,253-LOC monolith, and infrastructure code sits at the same level as domain packages.

**Goal:** Redesign ivy-lsp into a 3-layer architecture: `core/` (shared domain logic), `lsp/` + `mcp/` (thin protocol shells), and `infra/` (cross-cutting infrastructure). Clean break -- all imports updated, no backward-compat shims.

## Design Decisions

1. **`lsprotocol` is a domain dependency** -- `SymbolKind` and `DiagnosticSeverity` are used as data enums throughout parsing/, diagnostics/, analysis/. Accepted in `core/`. No native enum extraction needed.
2. **`adapters/` -> `core/adapters/`** (not infra/) -- adapters import from parsing/ and semantic/.
3. **`protocols.py` -> `core/protocols.py`** -- consumed by visualization files that take `IvyServerProtocol`.
4. **Visualization stays in `lsp/`** -- files take `IvyServerProtocol` as argument; cannot be pure core without refactoring the server coupling. Deferred to future work.
5. **`features/patterns.py` -> `core/analysis/`** -- zero LSP imports, consumed by MCP tools only.
6. **`features/coverage_hints.py` -> `core/coverage_hints.py`** -- zero LSP imports, consumed by MCP tools.
7. **`mcp_server.py` -> 2-way split** -- `start_mcp()` has 15 nonlocal closures sharing mutable state. Extract `ToolContext` to `mcp/context.py`, keep rest in `mcp/server.py`. First refactor closures into a `McpServerState` class, then split.
8. **`tools/__init__.py` keeps its role** as `mcp/tools/__init__.py` -- already contains registration logic.
9. **Drop most file renames** -- keep only: `diagnostics.py`->`publisher.py`, `diagnostic_compute.py`->`compute.py`, `_primitives.py`->`primitives.py`.
10. **Flatten `lsp/` sub-packages from 7 to 3** -- keep `navigation/`, `diagnostics/`, `ui/`. All other features as flat `lsp/` files.
11. **Split `observability/handlers.py`** -- `LspLogHandler` -> `lsp/lsp_log_handler.py`, non-LSP handlers stay in `infra/observability/`.
12. **`utils/structural_lint.py` -> `core/structural_lint.py`** -- imports from parsing/.
13. **Phased execution** -- 7 phases, each independently verifiable, separate git commits for moves vs import rewrites.

## Target Architecture

```
ivy_lsp/
+-- __init__.py                  # Version, top-level re-exports
+-- __main__.py                  # CLI entry: dispatches to lsp/ or mcp/ mode
|
+-- core/                        # Shared domain logic (may use lsprotocol enums)
|   +-- protocols.py             # IvyServerProtocol definition
|   +-- verification.py          # Shared Ivy verification functions
|   +-- coverage_hints.py        # Coverage hint computation (from features/)
|   +-- structural_lint.py       # Fast syntax validation (from utils/)
|   +-- parsing/                 # Ivy source parsing (3-tier: Parser->Lexer->Regex)
|   +-- semantic/                # Unified semantic model + analysis pipeline
|   +-- analysis/                # Requirements, patterns, formula analysis
|   |   +-- requirements/        # Consolidated requirement extraction
|   +-- indexer/                 # Workspace indexing + include resolution
|   +-- compilation/             # Ivy compiler subprocess management
|   +-- diagnostics/             # Structured error system (uses lsprotocol enums)
|   +-- workspace/               # Workspace detection, context, active workspace
|   +-- rfc/                     # RFC fetching, parsing, staleness
|   +-- adapters/                # External dependency adapters
|
+-- lsp/                         # LSP protocol shell (thin adapter)
|   +-- server.py                # IvyLanguageServer class
|   +-- server_setup.py          # ServerSetupMixin
|   +-- bulk_orchestrator.py     # BulkOrchestrationMixin
|   +-- index_builder.py         # Workspace indexing orchestrator
|   +-- pygls_patches.py         # pygls library patches
|   +-- lsp_log_handler.py       # LspLogHandler (split from observability/)
|   +-- [flat feature files]     # completion, rename, code_action, signature_help,
|   |                            # document_symbols, workspace_symbols, document_highlight,
|   |                            # commands, commands_extended, visualization, viz_*
|   +-- navigation/              # definition, implementation, references, call_hierarchy, hover
|   +-- diagnostics/             # publisher (renamed), compute (renamed)
|   +-- ui/                      # code_lens, folding_range, selection_range, monitoring, status
|
+-- mcp/                         # MCP protocol shell (thin adapter)
|   +-- server.py                # start_mcp(), McpServerState (from mcp_server.py)
|   +-- context.py               # ToolContext class (extracted from mcp_server.py)
|   +-- bridge.py, sidecar.py, client.py
|   +-- tools/                   # MCP tool implementations (from tools/)
|   +-- formatters/              # Markdown output formatters
|
+-- infra/                       # Cross-cutting infrastructure
    +-- config.py                # Configuration management
    +-- observability/           # Logging, tracing, session (minus LspLogHandler)
    +-- utils/                   # Helpers (minus structural_lint)
```

## Dependency Rules

- `infra/` -> nothing (no core/, lsp/, mcp/ imports)
- `core/` -> `infra/` only (+ lsprotocol enums allowed)
- `lsp/` and `mcp/` -> `core/` + `infra/`
- `lsp/` and `mcp/` MUST NOT cross-import each other

## Execution Phases

| Phase | Scope | Risk | Est. Import Changes |
|-------|-------|------|---------------------|
| 0 | Pre-flight: baseline tests, git tag, @patch inventory | None | 0 |
| 1 | Move infra/ (config, observability, utils) | Low | ~550 |
| 2 | Move core/ (parsing, semantic, analysis, indexer, ...) | Medium | ~900 |
| 3 | Move lsp/ (server, features -> flat + sub-packages) | Medium-High | ~500 |
| 4 | Move mcp/ (tools, formatters, bridge, sidecar) | Medium | ~200 |
| 5 | Refactor + split mcp_server.py (closure->class, then extract) | **Highest** | ~100 |
| 6 | Entry point, cleanup, full validation | Low | ~50 |

Each phase leaves the codebase in a working state with all tests passing. Git commits separate pure moves from import rewrites to preserve blame history.

## Verification

1. `pytest tests/ -x --tb=short` -- all ~1,965 tests pass after each phase
2. `pyright ivy_lsp/` -- no broken type references
3. Import graph validation -- enforce dependency rules
4. LSP smoke test: hover + go-to-definition on .ivy file
5. MCP smoke test: ivy_verify + ivy_model_info tools
6. Plugin smoke test: /nct-health

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| @patch() string paths silently target wrong module | High | Inventory in Phase 0; update in Phase 6 |
| mcp_server.py closure-to-class changes behavior | Critical | Phase 5 isolates this; test after each commit |
| ~1,700 import rewrites | High | Phased; automated find-replace; test after each |
| Circular deps (diagnostics <-> commands) | Medium | All land in lsp/; deferred imports preserved |
| Git blame loss | Medium | Separate commits for moves vs content changes |

## Summary

- ~80 files moved across 6 phases
- 3 files renamed, 2 files split
- ~12 new __init__.py files
- ~1,700 import lines updated
- No functionality changes -- pure structural refactoring
