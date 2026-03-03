# Changelog

All notable changes to ivy-lsp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-03-03

This is a collapsed entry covering all changes from v0.7.1 through v0.11.0.

### Breaking

- **Verification return schema**: `run_ivy_compile()` and `run_ivy_show()` return dicts now use `"raw_output"` instead of `"output"`. New keys added: `"diagnostics"`, `"diagnostic_count"`, `"error_summary"`. The MCP server JSON responses changed accordingly.
- **Unified diagnostic schema**: All three verification functions (`run_ivy_check`, `run_ivy_compile`, `run_ivy_show`) now return diagnostics with a `"source"` field (`"ivy_check"`, `"ivy_error"`, or `"cpp_compiler"`).

### Added

- **Unified error parsing** (`ivy_output.py`): `parse_ivy_output()` handles three error formats — standard ivy_check (`file:line: severity: message`), IvyError tracebacks (`ivy.ivy_utils.IvyError: ...`), and C++ compiler errors (`file:line:col: severity: message`). Deduplicates across formats.
- **Error formatting** (`ivy_output.py`): `format_ivy_error()` and `format_ivy_errors()` for human-readable display of Ivy parser errors, including raw tuples with nested include-chain locations.
- **Error summary extraction** (`ivy_output.py`): `extract_error_summary()` produces one-liner summaries prioritizing errors > warnings > raw output fallback.
- **Environment-variable timeouts**: `IVY_LSP_VERIFY_TIMEOUT`, `IVY_LSP_TOOL_COMPILE_TIMEOUT`, `IVY_LSP_SHOW_MODEL_TIMEOUT` with safety floors.
- **Shared verification module** (`verification.py`): Unified `run_ivy_check`, `run_ivy_compile`, `run_ivy_show` used by both LSP and MCP code paths.
- **Semantic model and analysis pipeline**: `SemanticModel`, RFC annotations, bracket-tag parsing, 3-tier analysis pipeline (T1: RFC annotations, T2: requirement extraction, T3: background compilation).
- **Scoped requirement model**: `ScopedRequirementModel` with per-test scoped queries, test scope detection, export/import extraction, and NCT classification.
- **New LSP features**: `foldingRange`, `documentHighlight`, `selectionRange`, `rename`, `signatureHelp`, `codeAction`, `ivy/setActiveTest`, `ivy/listTests`, `ivy/compileTest`, `ivy/recompileAll`, `ivy/activeDocumentChanged`, `ivy/compiledModel`.
- **Scope-aware code lenses and diagnostics**: Code lenses and diagnostics respect the active test scope with NCT classification counts.
- **MCP server mode**: `--mcp` flag starts a Model Context Protocol server exposing Ivy verification, compilation, and semantic tools.
- **Parallel workspace indexer**: Thread-pool-based parallel parsing for faster workspace indexing.
- **`textDocument/didClose` handler**: Clears stale diagnostics and cancels pending debounce/deep tasks on file close.
- **Per-file generation counter**: Discards stale T3 compilation results when source files change during background compilation.
- **`IvyServerProtocol`**: Typed protocol replacing `server: Any` in feature handler signatures.

### Changed

- **CodeLens commands**: Registered via `server.feature()` instead of `server.command()` to avoid `vscode-languageclient v9+` duplicate command conflicts.
- **Raw parser tuple handling**: `_convert_error_to_diagnostic` now formats raw parser tuples using `format_ivy_error()` and extracts line numbers from nested location tuples.
- **Deep diagnostics task tracking**: Tasks tracked in a dict and cancelled on file close or new save.
- **Document version**: `PublishDiagnosticsParams` now includes document version at all publish sites.
- **`did_change` handler**: Converted to async for pygls 2.x consistency.
- **Coverage hint diagnostics**: Use `DiagnosticTag.Unnecessary` and actual line length instead of magic `character=999`.
- Replaced deprecated `asyncio.get_event_loop()` with `get_running_loop()`.
- Replaced anonymous proxy types with named dataclasses.
- Eagerly create progress tokens outside `state_lock` to prevent nested locking.

### Fixed

- **Pipe-break stability**: Clean shutdown with pipe-break callback, background thread safety nets, and silent write cascade handling to prevent crashes on client disconnect.
- **Duplicate CodeLens command registration**: Removed `server.command()` registrations that caused VS Code startup crashes.
- **Thread safety**: Per-loop semaphore factory, atomic `add_action_if_absent`, `last_notify_time` under `_state_lock`, nested locking elimination in `to_status_dict`.
- **Concurrency**: `submitted_count` tracking for accurate partial-cancellation detection in bulk T3 compilation.
- **`_build_model` caching**: ImportError is cached to prevent repeated import attempts; nonlocal write race fixed.
- **Import/include handling**: Fixed `NameError: importer not defined` crash, parser state isolation (12 globals saved/restored), symbols attributed to original source file.

## [0.7.1] - 2026-02-26

### Added

- **`ivy/activeDocumentChanged` handler**: Auto-detects active test context when the user switches documents, keeping scoped diagnostics and code lenses in sync.
- **NCT classification tags in code lenses**: Scoped lens labels now show NCT classification counts via `get_scoped_nct_counts()`.
- **Diagnostic refresh on `ivy/setActiveTest`**: Diagnostics are automatically refreshed when the active test changes.

### Changed

- Added debug logging to the diagnostic refresh error handler.
- Cleaned up `get_scoped_nct_counts` code per code review findings.

## [0.7.0] - 2026-02-26

### Added

- **ScopedRequirementModel**: Replaces the flat `RequirementGraph` with per-test scoped queries, enabling test-specific code lenses and diagnostics.
- **TestScope and role detection**: `TestScope` dataclass with automatic role inference from export/import info.
- **Export/Import extraction**: Full-mode AST walking and light-mode regex fallback for `ExportDecl`/`ImportDecl` extraction.
- **ExportImportInfo data structure**: Typed representation of export/import relationships.
- **NCT classification**: Enums and functions for Network-Centric Compositional Testing classification of requirements.
- **New LSP commands**: `ivy/setActiveTest`, `ivy/listTests`, `ivy/compileTest` for test-scoped workflows.
- **Scope-aware diagnostics**: Unmonitored action detection now respects active test scope.
- **Scope-aware code lenses**: Code lenses use scoped queries when an active test is set.
- **ExportDecl/ImportDecl symbols**: Extracted as `Event` symbols in the document symbol provider.
- **Scope recomputation on reindex**: Test scopes are invalidated and recomputed when files are re-indexed.

## [0.5.4] - 2026-02-25

### Fixed

- **Progress token**: Use `server.work_done_progress` instead of `server.progress` for pygls 2.0.1 compatibility. Progress reporting now works instead of silently degrading.
- **Include resolution for tools**: `ivy/verify`, `ivy/compile`, and `ivy/showModel` now resolve filepaths through the staging directory, enabling cross-directory include resolution for Ivy CLI tools.

### Added

- `IncludeResolver.get_staged_path()`: Public API for staging directory path lookup.

## [0.5.2] - 2026-02-25

### Added

- **MCP server mode**: `--mcp` flag starts a Model Context Protocol server exposing Ivy verify/compile/show tools. Optional `mcp` extra dependency.

### Changed

- Updated README with configuration section documenting `IVY_LSP_INCLUDE_PATHS` and `IVY_LSP_EXCLUDE_PATHS` environment variables.
- Updated internal README with IncludeResolver architecture, workspace filtering design, and four-step include resolution documentation.
- Updated CHANGELOG with retroactive v0.5.1 entry.

## [0.5.1] - 2026-02-25

### Added

- **Include/exclude path filtering**: `IVY_LSP_INCLUDE_PATHS` and `IVY_LSP_EXCLUDE_PATHS` environment variables control workspace indexing scope.
- **IncludeResolver**: New `indexer/include_resolver.py` with 4-step include resolution (local → staging → workspace → stdlib), two-layer directory filtering (hardcoded + user-configurable), and flat staging directory with deterministic collision handling.
- **Staging directory**: Flat symlink layout mirroring `ivyc`'s `include/1.7/` model. First sorted path wins on filename collisions.

## [0.5.0] - 2026-02-25

### Added

- **LSP custom requests**: `ivy/verify`, `ivy/compile`, `ivy/showModel`, `ivy/capabilities` for triggering Ivy tools from VSCode.
- **Smart isolate detection**: Automatically detects the isolate under cursor using document symbols for scoped verification.
- **Progress reporting**: `$/progress` tokens for long-running operations with cancellation support.
- **Diagnostic publishing**: `ivy/verify` parses `ivy_check` output and publishes results to the Problems panel.
- Extracted reusable `parse_ivy_check_output()` from `run_deep_diagnostics` for shared use.

## [0.4.0] - 2026-02-25

### Added

- **Requirements analysis engine**: New `analysis/` module with graph-based dependency tracking for `require`, `ensure`, `assume`, and `assert` statements across Ivy files.
- **RequirementGraph**: Typed graph with 4 node types (RequirementNode, StateVarNode, ActionNode, PropertyNode) and 5 edge types (READS, WRITES, CONSTRAINS, DEPENDS_ON, PROPAGATED_FROM).
- **Full-mode requirement extractor**: Walks Ivy AST action bodies to extract requirements from `before`/`after` monitors and direct action bodies.
- **Light-mode requirement extractor**: Regex-based fallback when Z3/full parser is unavailable.
- **Formula analyzer**: Extracts state variable references from formula AST nodes and text.
- **Code lenses**: Inline annotations above monitors ("N require | M ensure | reads K state vars"), state variables ("read by N requirements across M files"), properties/axioms ("shares state with N requirements"), and include directives ("brings N requirements into scope").
- **Requirement diagnostics**: Include chain propagation (Info), unmonitored actions (Hint), and high-impact state variables (Info) reported as LSP diagnostics.
- 198 new unit tests covering all analysis modules, code lenses, and requirement diagnostics.

## [0.3.3] - 2026-02-25

### Fixed

- Fixed `NameError: name 'importer' is not defined` crash when parsing `.ivy` files containing `include` or `using` directives. The parser wrapper now sets `ip.importer` before calling `ip.parse()`, matching upstream `ivy_compiler` behavior.
- `ParserSession` now saves/restores `ip.importer` (12 globals total) to prevent state leakage between files.
- `ast_to_symbols` now filters out declarations originating from included files, ensuring symbols are attributed to their original source file for correct go-to-definition and workspace symbol lookup.

## [0.3.2] - 2026-02-25

### Fixed

- Registered `initialized` handler with pygls `@self.feature(lsp.INITIALIZED)` decorator. Previously defined as a plain method that pygls never called, so workspace indexing never ran and all LSP features (go-to-definition, references, hover, completion) returned nothing.

## [0.3.1] - 2026-02-25

### Fixed

- Fixed pygls API calls: `publish_diagnostics(uri, diags)` → `text_document_publish_diagnostics(PublishDiagnosticsParams(...))` (4 call sites in diagnostics.py).
- Fixed `show_message_log(...)` → `window_log_message(LogMessageParams(...))` in server.py.

### Changed

- CI now installs z3 via `pip install -e ".[dev,full]"`.

## [0.3.0] - 2026-02-25

### Added

- Light mode: z3 made optional with `FallbackOnlyParser` when z3 is unavailable.
- Keyword completion falls back to a frozen list when `ivy.ivy_lexer` is not importable.

## [0.2.1] - 2026-02-25

### Added

- Initial release with full LSP features: document symbols, workspace symbols, go-to-definition, find references, hover, completion, and diagnostics.
- Workspace indexer with include resolution.
- Dual parser: full `IvyParserWrapper` (z3) and `FallbackScanner` (lexer-only error recovery).

[0.11.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.11.0
[0.7.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.7.1
[0.7.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.7.0
[0.5.4]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.5.4
[0.5.2]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.5.2
[0.5.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.5.1
[0.5.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.5.0
[0.4.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.4.0
[0.3.3]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.3
[0.3.2]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.2
[0.3.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.1
[0.3.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.0
[0.2.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.2.1
