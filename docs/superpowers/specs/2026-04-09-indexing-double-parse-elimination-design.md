# Indexing Double-Parse Elimination

**Date:** 2026-04-09
**Scope:** ivy-lsp indexing pipeline performance optimization
**Target:** ~3x speedup on full `--force --all` index builds (572s → ~170s for APT/331 files)

## Problem

The offline index builder (`index_builder.py`) parses every `.ivy` file twice:

1. **Phase B** (`_extract_parallel`): `TieredExtractor.extract()` on all files, parallelized across N workers via `ProcessPoolExecutor`. For APT (331 files, 8 workers): ~130s.
2. **Semantic model build** (`build_semantic_model` via `_build_models`): `TieredExtractor.extract()` on the same files again, sequentially in a single thread. For APT: ~442s.

Both phases use the same resolver configuration and produce identical extraction results (symbols, includes, references). The second parse is pure redundancy.

The cost comes from Tier 1 parsing: `IvyParserWrapper.parse()` invokes `ivy.ivy_parser.parse()`, which recursively resolves every `include` directive via `_lsp_importer`. APT/QUIC files have 10+ levels of include depth, causing each file to trigger dozens of recursive sub-parses.

## Solution: Feed Pre-Computed Extraction Results Into the Model Builder

Pass Phase B's extraction results (symbols, includes) into `build_semantic_model` so it skips re-extraction. The model builder's per-file loop changes from "parse + annotate + populate" to "deserialize + annotate + reference-regex + populate."

### Data flow

**Current:**
```
Phase B (_extract_parallel)          Semantic model build
┌─────────────────────┐              ┌─────────────────────────┐
│ TieredExtractor     │──symbols──→  │ TieredExtractor         │ ← REDUNDANT
│ .extract()          │  includes    │ .extract()              │
│ (8 workers)         │  exports     │ + RFC annotations       │
│ ~130s               │  reqs        │ + populate_model        │
└─────────────────────┘              │ + wire edges            │
                                     │ ~442s (sequential)      │
                                     └─────────────────────────┘
```

**Proposed:**
```
Phase B (_extract_parallel)          Semantic model build
┌─────────────────────┐              ┌─────────────────────────┐
│ TieredExtractor     │──symbols──→  │ Deserialize IvySymbol   │
│ .extract()          │  includes    │ + RFC annotations       │
│ (8 workers)         │  exports     │ + reference regex       │
│ ~130s               │  reqs        │ + populate_model        │
└─────────────────────┘              │ + wire edges            │
                                     │ ~20-40s (sequential)    │
                                     └─────────────────────────┘
```

## Detailed Design

### 1. New dataclass in `core/semantic/model_builder.py`

```python
@dataclass
class PrecomputedFileData:
    symbols: list[dict]      # IvySymbol dicts from to_dict()
    includes: list[str]      # raw include names
    tier_used: int           # 1, 2, or 3
```

### 2. API change to `build_semantic_model`

Add one optional parameter:

```python
def build_semantic_model(
    root: str,
    find_files_fn: Callable[[str], list[str]],
    include_resolver: Any | None = None,
    stdlib_modules: frozenset[str] | None = None,
    precomputed_extractions: dict[str, PrecomputedFileData] | None = None,  # NEW
) -> Optional[Any]:
```

Per-file loop behavior when `precomputed_extractions` is provided and the file's absolute path is found in the dict:

1. Read source from disk (unchanged, needed for RFC annotations).
2. `parse_file_rfc_annotations(source, abs_path)` (unchanged).
3. **Skip** `extractor.extract(source, abs_path)`.
4. Deserialize symbols: `[IvySymbol.from_dict(d) for d in pre.symbols]`.
5. Extract references: `extract_references_regex(source, symbols)` (cheap regex, same patterns as TieredExtractor uses internally).
6. `populate_model_from_symbols(model, symbols, abs_path, tier_used=pre.tier_used)`.
7. Store includes and references for edge wiring (unchanged).

When `precomputed_extractions` is `None`, current behavior is preserved exactly. The `TieredExtractor` is only instantiated when pre-computed data is unavailable.

### 3. Integration in `_build_models` (index_builder.py)

Expand `_build_models` signature to receive Phase B data:

```python
def _build_models(
    self,
    protocol_dir, protocol, resolver, ivy_files,
    requirements_map, scopes,
    symbols_map: Dict[str, list],        # NEW
    includes_raw: Dict[str, List[str]],  # NEW
    manifest_files: Dict[str, dict],     # NEW (for tier info)
) -> tuple:
```

Build the `precomputed_extractions` dict from Phase B data:

```python
precomputed = {}
for rel_path, syms in symbols_map.items():
    abs_path = os.path.join(protocol_dir, rel_path)
    tier_label = manifest_files.get(rel_path, {}).get("parse_tier", "unknown")
    tier_num = {"ast": 1, "lexer": 2, "regex": 3}.get(tier_label, 3)
    precomputed[abs_path] = PrecomputedFileData(
        symbols=syms,
        includes=includes_raw.get(rel_path, []),
        tier_used=tier_num,
    )
```

Pass to `build_semantic_model`:

```python
semantic_model = build_semantic_model(
    root=protocol_dir,
    find_files_fn=_find_files,
    include_resolver=resolver.resolve,
    precomputed_extractions=precomputed,
)
```

Update `build_protocol` call site (~line 655) to pass the three additional maps.

### 4. Callers not affected

- **`mcp/model_builder.py:104`**: Continues calling `build_semantic_model` without `precomputed_extractions`. Falls back to full extraction (current behavior).
- **`AnalysisPipeline`**: Does not call `build_semantic_model` directly.

## Correctness Guarantees

### Why the output is equivalent

- **Symbols**: `IvySymbol.to_dict()` → `IvySymbol.from_dict()` is lossless. All 7 fields (name, kind, range, children, detail, file_path, synthetic) round-trip cleanly.
- **Includes**: Plain string lists, passed directly from Phase B.
- **References**: Extracted via `extract_references_regex`, the same regex patterns (`_CALL_STMT_RE`, `_INSTANCE_RE`, `_MONITOR_RE`) used internally by TieredExtractor tiers 2 and 3. Tier 1 also uses these for reference extraction.
- **Resolver consistency**: Both phases use the same resolver instance (or serialized equivalent). The staging directory and staged file map are transferred via `to_config_dict()`.
- **RFC annotations**: Still extracted from source text in the model builder (unchanged).
- **Edge wiring**: Deterministic given the same nodes, includes, and references.

### What could differ

`semantic_model.pickle.gz` may not be byte-identical due to Python's hash randomization affecting dict/set iteration order during model construction. The logical content (same nodes, same edges, same names) is identical.

## Verification

### Integration test

A new test that builds the semantic model both ways and compares at the semantic level:

```python
def test_precomputed_matches_full_build(protocol_dir):
    model_full = build_semantic_model(
        root=protocol_dir,
        find_files_fn=find_files,
        include_resolver=resolver.resolve,
        precomputed_extractions=None,
    )
    model_pre = build_semantic_model(
        root=protocol_dir,
        find_files_fn=find_files,
        include_resolver=resolver.resolve,
        precomputed_extractions=precomputed,
    )
    assert_same_nodes(model_full, model_pre)   # count, IDs, names, types
    assert_same_edges(model_full, model_pre)   # count, edge types, targets
```

### CLI smoke check

Run `python -m ivy_lsp index --force --all --workers 8` before and after. Diff `.ivy-index/` outputs:
- `symbols.json`, `includes.json`, `exports.json`, `requirements.json`: byte-identical (Phase B unchanged).
- `semantic_model.pickle.gz`: load both, compare node/edge sets.

## Files Modified

| File | Change |
|------|--------|
| `core/semantic/model_builder.py` | Add `PrecomputedFileData`. Add `precomputed_extractions` parameter. Conditional per-file loop. |
| `lsp/index_builder.py` | Expand `_build_models` to accept and forward Phase B data. Update `build_protocol` call site. |
| New test file | Integration test comparing both code paths. |

## Files NOT Modified

- `_extract_parallel`, `_extract_one_file`, `FileExtractionResult` (Phase B untouched)
- `mcp/model_builder.py` (continues using `build_semantic_model` without pre-computed data)
- `TieredExtractor`, `IvyParserWrapper`, `ParserSession` (parsing internals untouched)
- All `.ivy-index/` output artifacts except `semantic_model.pickle.gz` (byte-identical)

## Risk Assessment

**Risk level: Low.**

- Backward-compatible API change (optional parameter, default `None`).
- Three files modified, one new test.
- Same data, different flow. Round-trip serialization is lossless.
- MCP server and other callers unaffected.
- Verification test ensures structural equivalence.
