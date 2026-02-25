# ivy-lsp

Language Server Protocol (LSP) implementation for the [Ivy](https://github.com/kenmcmil/ivy) formal specification language.

## Features

- **Document Symbols** -- outline view of types, objects, actions, modules, properties
- **Workspace Symbols** -- search symbols across all `.ivy` files
- **Go-to-Definition** -- jump to symbol definitions across `include` boundaries
- **Find References** -- locate all usages of a symbol in the workspace
- **Completion** -- context-aware suggestions (after `.`, `include`, keywords, symbols in scope)
- **Hover** -- type signatures and symbol details on mouse hover
- **Diagnostics** -- parse errors, missing `#lang` directive, unresolved includes, unmatched braces

## Installation

```bash
pip install ivy-lsp
```

Or as an optional extra of `panther_ms_ivy`:

```bash
pip install panther_ms_ivy[lsp]
```

## Usage

Start the language server over stdio:

```bash
python -m ivy_lsp
```

Or via the installed console script:

```bash
ivy_lsp
```

### With VS Code

Install the [Ivy Language](https://marketplace.visualstudio.com/items?itemName=elniak.ivy-language) extension, which automatically starts `ivy-lsp` for `.ivy` files.

### With Serena

Add `ivy` to the `languages` list in `.serena/project.yml`:

```yaml
languages:
  - python
  - ivy
```

## Configuration

When running standalone (outside VS Code), configure workspace indexing via environment variables:

| Variable | Description |
|----------|-------------|
| `IVY_LSP_INCLUDE_PATHS` | Comma-separated directories to include (whitelist). When set, only these directories are scanned. |
| `IVY_LSP_EXCLUDE_PATHS` | Comma-separated directories to exclude, additive to the hardcoded exclusion list. |

When using the [VS Code extension](https://marketplace.visualstudio.com/items?itemName=elniak.ivy-language), these are set automatically from the `ivy.lsp.includePaths` and `ivy.lsp.excludePaths` settings.

Standalone example:

```bash
IVY_LSP_EXCLUDE_PATHS=submodules,test python -m ivy_lsp
```

## Requirements

- Python 3.10+
- `panther_ms_ivy` (Ivy parser and compiler)
- `pygls` (Python LSP framework)

## Development

```bash
git clone https://github.com/ElNiak/ivy-lsp.git
cd ivy-lsp
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
