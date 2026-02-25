# Ivy Language Server (ivy_lsp)

Language Server Protocol implementation for the Ivy formal specification language.

## Installation

From the panther_ivy directory:

```bash
pip install -e ".[lsp]"
```

This installs `pygls` and `lsprotocol` as LSP dependencies.

## Usage

### Standalone (stdio)

```bash
python -m ivy_lsp
# or
ivy_lsp
```

The server communicates over stdio using JSON-RPC (LSP protocol).

### With Serena MCP

1. Add `ivy` to `.serena/project.yml` languages list
2. Ensure `ivy_lsp` is installed in the Serena Python environment
3. Serena will automatically start the Ivy LSP for `.ivy` files

### With VSCode

Install the `vscode-ivy` extension (see `vscode-ivy/README.md`).

## Supported LSP Features

| Feature | Method | Description |
|---------|--------|-------------|
| Document Symbols | `textDocument/documentSymbol` | File outline with nested hierarchy |
| Workspace Symbols | `workspace/symbol` | Cross-file symbol search |
| Go to Definition | `textDocument/definition` | Jump to symbol definition |
| Find References | `textDocument/references` | Find all uses of a symbol |
| Hover | `textDocument/hover` | Type signatures and documentation |
| Completion | `textDocument/completion` | Context-aware suggestions |
| Diagnostics | `textDocument/publishDiagnostics` | Parse errors, structural warnings |

## Architecture

```
Source text
    |
    v
IvyParserWrapper (ivy_parser + state isolation)
    |
    +--[success]--> ast_to_symbols() --> IvySymbol tree
    |
    +--[failure]--> fallback_scan() --> IvySymbol tree (degraded)
    |
    v
IncludeResolver (include/exclude filtering + flat staging)
    |
    v
WorkspaceIndexer (SymbolTable + IncludeGraph + FileCache)
    |
    v
LSP Feature Handlers (document symbols, definition, references, etc.)
```

### Key Components

- **`parsing/parser_session.py`**: State isolation for the Ivy PLY parser
- **`parsing/ast_to_symbols.py`**: AST to IvySymbol conversion (13+ declaration types)
- **`parsing/fallback_scanner.py`**: Lexer-based fallback for broken files
- **`indexer/include_resolver.py`**: Include resolution with 4-step search, two-layer directory filtering, and flat staging
- **`indexer/workspace_indexer.py`**: Cross-file symbol table with include graph
- **`features/`**: LSP request handlers

## Workspace Filtering

### Four-Step Include Resolution

When resolving `include X`, the resolver searches in order:

1. **Same directory** as the including file (local overrides take priority)
2. **Staging directory** (flat symlink dir for unambiguous basename lookup)
3. **Workspace root** (fallback for top-level files)
4. **Standard library** (`ivy/include/1.7/`, highest version detected at runtime)

This order ensures project-specific files take priority over library defaults, and the staging directory provides flat disambiguation across deeply nested trees.

### Two-Layer Directory Exclusion

**Layer 1 — Hardcoded basename set** (`_EXCLUDED_DIR_BASENAMES`, O(1) `frozenset` lookup):
`build`, `dist`, `.git`, `.hg`, `.svn`, `node_modules`, `__pycache__`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.venv`, `venv`, `submodules`, `test`, plus glob patterns `pytest-of-*` and `pytest-*`.

These are pruned in-place during `os.walk` via `dirnames[:] = [...]`, which prevents the filesystem from ever descending into excluded subtrees (avoids thousands of unnecessary `stat` calls in large workspaces).

**Layer 2 — User-configurable relative paths** (`exclude_paths`, set via `IVY_LSP_EXCLUDE_PATHS`):
Matched against `os.path.relpath(dirpath, workspace_root)` with prefix semantics (`foo` matches `foo/bar/baz`). When a match is found, `dirnames.clear()` halts traversal into that subtree entirely.

### Include Paths (Whitelist Mode)

When `include_paths` is set (via `IVY_LSP_INCLUDE_PATHS`), only those subdirectories of the workspace root are walked. Empty means scan the entire workspace root. Both exclusion layers still apply within included paths. This allows users in large monorepos to scope indexing to relevant protocol directories.

### Flat Staging Directory

`create_staging_directory()` symlinks all discovered `.ivy` files into a single temporary directory, mirroring how `ivyc` prepares its `include/1.7/` layout. When multiple source files share the same basename, the first one in sorted path order wins (deterministic collision handling). This provides unambiguous basename-based include resolution regardless of directory depth.

### Configuration Transport

Environment variables `IVY_LSP_INCLUDE_PATHS` and `IVY_LSP_EXCLUDE_PATHS` use comma-separated format — a simple, widely-supported encoding that avoids JSON serialization overhead when the VS Code extension spawns the Python process.

### Restart-on-Change

When the VS Code extension detects a change to `includePaths` or `excludePaths` settings, it performs a full server restart rather than incremental re-index. This is simpler and guarantees consistency: the IncludeResolver and staging directory are rebuilt from scratch.

## Known Limitations

- Requires the `ivy` package on Python path (usually via `pip install -e .`)
- First workspace indexing may take several seconds for large projects (667+ files)
- Fallback scanner produces degraded symbol detail (no type signatures)
- Deep diagnostics (`ivy_check`) require `ivyc` on PATH (graceful degradation if absent)

## Troubleshooting

**"Import ivy failed"**: Ensure the panther_ivy package is installed (`pip install -e .`)

**Slow startup**: First workspace indexing parses all `.ivy` files. Subsequent file changes use incremental re-indexing.

**No diagnostics**: Parser diagnostics are always available. `ivy_check` diagnostics require the `ivyc` binary on PATH.
