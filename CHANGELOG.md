# Changelog

All notable changes to ivy-lsp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.3.2]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.2
[0.3.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.1
[0.3.0]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.3.0
[0.2.1]: https://github.com/ElNiak/ivy-lsp/releases/tag/v0.2.1
