# Diagnostic Scope and Lint Fixes

**Date:** 2026-04-10
**Scope:** Four ivy-lsp diagnostic bugs — false positives from workspace scoping failures and a missing lint rule.

## Bug 1: Unresolved Include False Positives

**Root cause:** `compute.py:64-65` selects `resolve_partitioned` as the callback when `_partition_staging` is a non-empty dict. During early indexing, the partition map is incomplete — files not yet mapped to a partition fail to resolve even though they exist in the workspace.

**Fix:** Use a fallback wrapper instead of `resolve_partitioned` directly. The wrapper tries `resolve_partitioned` first, then falls back to `resolver.resolve` (full workspace search) when the partitioned lookup returns `None`.

**Files:**
- `ivy_lsp/lsp/diagnostics/compute.py:59-67` — replace callback selection with fallback wrapper

**Correctness:** The partitioned resolver is a subset of the full resolver. Falling back to the full resolver can only produce correct results — it will never resolve an include to a file that isn't in the workspace. The only tradeoff is that the fallback may resolve to a file in a different partition, but for structural lint (which just checks existence), this is acceptable.

## Bug 2: Collision Count Not Layer-Filtered

**Root cause:** The workspace context metadata reports global collision counts from `resolver._collision_map`, which covers all files across all protocols in `protocol-testing/`. For a quic workspace with 11 layer paths, this inflates the count to 189 (includes cross-protocol duplicates that are never visible to the active workspace).

**Fix:** Filter the collision map to only count collisions where at least two colliding files fall within the active workspace's `include_paths` or `workspace_layers`. The filtering happens in the context builder where the collision count is computed, not in the resolver itself.

**Files:**
- `ivy_lsp/core/workspace/context.py:182-198` — filter `_collision_map` entries by active layer paths

**Correctness:** This is a display/metadata change. No resolution logic is affected.

## Bug 3: Cross-Workspace Shadow Diagnostics

**Root cause:** `offline_index_loader.py:112-127` merges all protocol semantic models into one global `SemanticModel` via `merge_from()`. Then `compute.py:387-420` queries `model._nodes_by_name` globally, reporting shadows across workspace boundaries (e.g., bgp symbols shadowing quic symbols).

**Fix:** Add a path-based protocol extraction helper and filter `other_nodes` at `compute.py:394` to only include files from the same protocol as the file being diagnosed.

```python
def _protocol_from_path(filepath: str) -> str | None:
    """Extract protocol name from a file path.

    Looks for 'protocol-testing/{protocol}/' in the path.
    Returns None if the path doesn't match this layout.
    """
    marker = "protocol-testing/"
    idx = filepath.find(marker)
    if idx < 0:
        return None
    rest = filepath[idx + len(marker):]
    return rest.split("/")[0] if "/" in rest else None
```

Then the shadow detection at line 394 becomes:

```python
local_proto = _protocol_from_path(abs_path)
other_nodes = [
    n for n in sym_nodes
    if n.file != abs_path and _protocol_from_path(n.file) == local_proto
]
```

When `local_proto` is `None` (file not under `protocol-testing/`), `_protocol_from_path(n.file) == None` will match all files with no protocol prefix, preserving current behavior for non-standard layouts.

**Files:**
- `ivy_lsp/lsp/diagnostics/compute.py:387-420` — add helper, filter `other_nodes`

**Correctness:** Shadows within the same protocol are still reported. Cross-protocol shadows are suppressed. This matches the user's expectation: each protocol workspace is independent.

## Bug 4: Missing Lowercase Parameter Lint

**Root cause:** Ivy's `ivy_logic_parser.py:13` treats identifier casing as semantic: uppercase-initial → `Variable` (universally quantified, no declaration needed), lowercase-initial → `Constant` (looked up in declared symbols, fails if not found). Declarations like `relation foo(src:t)` cause "unknown symbol: src" at compile time because `src` is treated as a constant reference.

No structural lint currently catches this before `ivy_check`, which is expensive and only runs inside Docker.

**Fix:** Add a new check in `structural_lint.py` that scans `relation` and `function` declarations for lowercase-initial parameter names.

Detection regex for parameter lists in relation/function declarations:
```python
_DECL_PARAM_RE = re.compile(
    r"^\s*(?:relation|function)\s+[\w.]+\s*\(([^)]+)\)", re.MULTILINE
)
```

For each match, split the parameter list by `,`, extract the parameter name (before `:`), and check if it starts with a lowercase letter.

Diagnostic:
- Code: `ivy.declaration.lowercaseParam`
- Severity: `Error`
- Message: `"Parameter '{name}' in {kind} declaration must start with uppercase (Ivy treats lowercase as constant references)"`
- Source: `ivy-lint`

Also register in `diagnostics/codes.py` with `DiagnosticSeverity.Error`.

**Files:**
- `ivy_lsp/core/structural_lint.py` — add `check_lowercase_params` function
- `ivy_lsp/core/diagnostics/codes.py` — register `ivy.declaration.lowercaseParam`
- `ivy_lsp/lsp/diagnostics/compute.py:42-94` — call `check_lowercase_params` from `check_structural_issues`

**Correctness:** Actions use lowercase parameter names legitimately (action parameters are concrete, not universally quantified). Only `relation` and `function` declarations require uppercase parameters. The regex targets only these two declaration kinds.

## Files Summary

| File | Bugs | Change |
|------|------|--------|
| `ivy_lsp/lsp/diagnostics/compute.py` | 1, 3 | Fallback resolver wrapper; protocol-scoped shadow filter |
| `ivy_lsp/core/workspace/context.py` | 2 | Filter collision count to active layers |
| `ivy_lsp/core/structural_lint.py` | 4 | Add `check_lowercase_params` |
| `ivy_lsp/core/diagnostics/codes.py` | 4 | Register `ivy.declaration.lowercaseParam` |

## Testing

Each bug gets one or more unit tests:

1. **Bug 1:** Test that `check_structural_issues` resolves an include when partitioned staging is incomplete (mock `resolve_partitioned` returning `None`, `resolve` returning a path).
2. **Bug 2:** Test that collision count excludes cross-protocol duplicates when layer paths are set.
3. **Bug 3:** Test that shadow detection only reports shadows within the same protocol. Two symbols with the same name in different protocols → no diagnostic. Same name within one protocol → diagnostic.
4. **Bug 4:** Test that `check_lowercase_params` flags `relation foo(src:t)` as error, accepts `relation foo(Src:t)`, and does not flag `action bar(src:t)`.
