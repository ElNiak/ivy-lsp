# @patch() Path Inventory for Package Redesign

**Date:** 2026-03-25
**Baseline:** 2622 passed, 8 failed, 38 skipped, 4 errors (pre-package-redesign tag: `01bef96`)

## Summary

- **21 unique patch targets** across 15 test files
- **82 total patch call sites** that will need updating
- **14 targets change path** (Phases 1-5)
- **7 targets are unchanged** (stay in `__main__` or move within the same rename)

## Inventory

### Phase 1: infra/ moves (config, observability, utils)

No @patch targets affected. None of the patched paths reference `config.py`, `observability/`, or `utils/`.

### Phase 2: core/ moves (parsing, semantic, indexer, compilation, workspace, verification)

| # | Current Path | New Path | Sites | Test File(s) |
|---|---|---|---|---|
| 1 | `ivy_lsp.parsing.ast_to_symbols.ast_to_symbols` | `ivy_lsp.core.parsing.ast_to_symbols.ast_to_symbols` | 3 | test_deep_index_progress.py, test_demand_deep_parse.py |
| 2 | `ivy_lsp.parsing.fallback_scanner.fallback_scan` | `ivy_lsp.core.parsing.fallback_scanner.fallback_scan` | 2 | test_task_3_2_diagnostics.py |
| 3 | `ivy_lsp.parsing.tiered_extractor.TieredExtractor._try_lexer` | `ivy_lsp.core.parsing.tiered_extractor.TieredExtractor._try_lexer` | 2 | test_tiered_extractor.py |
| 4 | `ivy_lsp.parsing.token_stream.tokenize_ivy` | `ivy_lsp.core.parsing.token_stream.tokenize_ivy` | 2 | test_task_2_3_workspace_indexer.py |
| 5 | `ivy_lsp.semantic.analysis_pipeline.parse_file_rfc_annotations` | `ivy_lsp.core.semantic.analysis_pipeline.parse_file_rfc_annotations` | 2 | test_analysis_pipeline.py |
| 6 | `ivy_lsp.indexer.file_cache.os.path.getmtime` | `ivy_lsp.core.indexer.file_cache.os.path.getmtime` | 2 | test_task_2_2_file_cache.py |
| 7 | `ivy_lsp.indexer.scope_manager.extract_exports_imports_full` | `ivy_lsp.core.indexer.scope_manager.extract_exports_imports_full` | 2 | test_workspace_indexer_scoping.py |
| 8 | `ivy_lsp.indexer.scope_manager.extract_exports_imports_light` | `ivy_lsp.core.indexer.scope_manager.extract_exports_imports_light` | 4 | test_workspace_indexer_scoping.py |
| 9 | `ivy_lsp.compilation.compiler_manager.multiprocessing` | `ivy_lsp.core.compilation.compiler_manager.multiprocessing` | 4 | test_compiler_manager.py |
| 10 | `ivy_lsp.verification.run_ivy_subprocess` | `ivy_lsp.core.verification.run_ivy_subprocess` | 24 | test_verification.py |
| 11 | `ivy_lsp.workspace.context.WorkspaceContext.detect` | `ivy_lsp.core.workspace.context.WorkspaceContext.detect` | 2 | test_index_cli.py |

**Subtotal:** 11 targets, 49 patch sites

### Phase 3: lsp/ moves (features, index_builder, server)

| # | Current Path | New Path | Sites | Test File(s) |
|---|---|---|---|---|
| 12 | `ivy_lsp.features.document_symbols.compute_document_symbols` | `ivy_lsp.lsp.document_symbols.compute_document_symbols` | 4 | test_commands.py |
| 13 | `ivy_lsp.features.diagnostics.compute_diagnostics` | `ivy_lsp.lsp.diagnostics.compute.compute_diagnostics` | 10 | test_active_test_commands.py |
| 14 | `ivy_lsp.features.visualization._get_requirement_graph` | `ivy_lsp.lsp.visualization._get_requirement_graph` | 2 | test_smart_suggestions_context.py |
| 15 | `ivy_lsp.index_builder.cli_index` | `ivy_lsp.lsp.index_builder.cli_index` | 2 | test_index_cli.py |

**Subtotal:** 4 targets, 18 patch sites

### Phase 4: mcp/ moves (tools, bridge, sidecar, server)

| # | Current Path | New Path | Sites | Test File(s) |
|---|---|---|---|---|
| 16 | `ivy_lsp.mcp_server.sidecar_client` | `ivy_lsp.mcp.server.sidecar_client` | 4 | test_lazy_bridge_integration.py, test_sidecar_monitor.py |
| 17 | `ivy_lsp.mcp_server.shared_ivy_check` | `ivy_lsp.mcp.server.shared_ivy_check` | 1 | test_mcp_verification_wiring.py |
| 18 | `ivy_lsp.sidecar_client._fetch_health` | `ivy_lsp.mcp.client._fetch_health` | 4 | test_sidecar_client.py |
| 19 | `ivy_lsp.tools.verification.shared_ivy_compile` | `ivy_lsp.mcp.tools.verification.shared_ivy_compile` | 2 | test_tools_verification.py |

**Subtotal:** 4 targets, 11 patch sites

### Unchanged (no path change needed)

| # | Current Path | New Path | Sites | Test File(s) |
|---|---|---|---|---|
| 20 | `ivy_lsp.__main__._setup_log_rotation` | `ivy_lsp.__main__._setup_log_rotation` | 1 | test_index_cli.py |
| 21 | `ivy_lsp.__main__.sys` | `ivy_lsp.__main__.sys` | 3 | test_index_cli.py |

**Subtotal:** 2 targets, 4 patch sites

## Risk Notes

1. **Highest-impact target:** `ivy_lsp.verification.run_ivy_subprocess` with 24 sites in test_verification.py. A typo in the new path will cause 24 test failures.
2. **Phase 3 diagnostics rename:** `diagnostics.py` is renamed to `compute.py` within `lsp/diagnostics/`, so the path changes from `features.diagnostics` to `lsp.diagnostics.compute`. This is both a move AND a rename.
3. **Phase 5 (mcp_server.py split):** The closures currently patched as `ivy_lsp.mcp_server.shared_ivy_check` and `ivy_lsp.mcp_server.sidecar_client` will move to `ivy_lsp.mcp.server.*`. If Phase 5 further refactors these into `McpServerState`, the patch targets may need a second update. Monitor during Phase 5.
4. **`os.path.getmtime` patch:** This patches a stdlib function imported into `ivy_lsp.indexer.file_cache`. The new path `ivy_lsp.core.indexer.file_cache.os.path.getmtime` follows the same pattern -- patching where the name is looked up, not where it's defined.

## Checklist

Use this checklist during each phase to verify all patch targets have been updated:

- [ ] Phase 2: Update 11 targets (49 sites) in 8 test files
- [ ] Phase 3: Update 4 targets (18 sites) in 3 test files
- [ ] Phase 4: Update 4 targets (11 sites) in 4 test files
- [ ] Phase 6: Final sweep -- re-run grep to confirm zero stale `ivy_lsp.{old_path}` references in tests/
