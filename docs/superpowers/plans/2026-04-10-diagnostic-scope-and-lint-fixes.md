# Diagnostic Scope and Lint Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four diagnostic bugs: unresolved include false positives, inflated collision counts, cross-workspace shadow diagnostics, and missing lowercase parameter lint.

**Architecture:** Four independent bug fixes in overlapping files. Bug 1 and 3 modify `compute.py`, Bug 2 modifies `mcp/context.py`, Bug 4 adds a new lint check in `structural_lint.py`. Each fix is self-contained and testable independently.

**Tech Stack:** Python 3.10+, lsprotocol, regex, pytest

**Spec:** `docs/superpowers/specs/2026-04-10-diagnostic-scope-and-lint-fixes-design.md`

---

## File Structure

| File | Role | Action |
|------|------|--------|
| `ivy_lsp/lsp/diagnostics/compute.py` | Diagnostic computation | Modify: fallback resolver (Bug 1), protocol-scoped shadows (Bug 3), call lowercase check (Bug 4) |
| `ivy_lsp/mcp/context.py` | MCP tool context builder | Modify: filter collision count (Bug 2) |
| `ivy_lsp/core/structural_lint.py` | Structural lint checks | Modify: add `check_lowercase_params` (Bug 4) |
| `ivy_lsp/core/diagnostics/codes.py` | Diagnostic code registry | Modify: register `ivy.declaration.lowercaseParam` (Bug 4) |
| `tests/test_structural_lint.py` | Structural lint tests | Modify: add lowercase param tests (Bug 4) |
| `tests/test_diagnostic_scope.py` | Scope-related diagnostic tests | Create: tests for Bugs 1, 2, 3 |

---

### Task 1: Fix cross-workspace shadow diagnostics (Bug 3)

**Files:**
- Modify: `ivy_lsp/lsp/diagnostics/compute.py:387-420`
- Test: `tests/test_diagnostic_scope.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_diagnostic_scope.py`:

```python
"""Tests for workspace-scoped diagnostic filtering."""

import pytest
from lsprotocol import types as lsp

from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import SymbolNode


@pytest.mark.unit
class TestShadowDiagnosticScoping:
    """Bug 3: Shadow diagnostics must not cross workspace boundaries."""

    def _make_model(self, nodes):
        model = SemanticModel()
        for n in nodes:
            model.add_node(n)
        return model

    def test_cross_protocol_shadow_suppressed(self):
        """Two symbols with same name in different protocols produce no shadow diagnostic."""
        from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics

        model = self._make_model([
            SymbolNode(
                id="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy:61:show_connected",
                name="show_connected",
                qualified_name="show_connected",
                kind="action",
                file="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy",
                line=60,
            ),
            SymbolNode(
                id="/ws/protocol-testing/quic/quic_tests/quic_test.ivy:256:show_connected",
                name="show_connected",
                qualified_name="show_connected",
                kind="action",
                file="/ws/protocol-testing/quic/quic_tests/quic_test.ivy",
                line=255,
            ),
        ])

        source = "#lang ivy1.7\n" + "\n" * 60 + "action show_connected\n"
        filepath = "/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy"

        diags = compute_semantic_diagnostics(model, filepath, source)
        shadow_diags = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert len(shadow_diags) == 0

    def test_same_protocol_shadow_reported(self):
        """Two symbols with same name in the same protocol produce a shadow diagnostic."""
        from ivy_lsp.lsp.diagnostics.compute import compute_semantic_diagnostics

        model = self._make_model([
            SymbolNode(
                id="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy:61:show_connected",
                name="show_connected",
                qualified_name="show_connected",
                kind="action",
                file="/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy",
                line=60,
            ),
            SymbolNode(
                id="/ws/protocol-testing/bgp/bgp_utils/helpers.ivy:10:show_connected",
                name="show_connected",
                qualified_name="show_connected",
                kind="action",
                file="/ws/protocol-testing/bgp/bgp_utils/helpers.ivy",
                line=9,
            ),
        ])

        source = "#lang ivy1.7\n" + "\n" * 60 + "action show_connected\n"
        filepath = "/ws/protocol-testing/bgp/bgp_shims/bgp_shim.ivy"

        diags = compute_semantic_diagnostics(model, filepath, source)
        shadow_diags = [d for d in diags if d.code == "ivy.include.shadowDeclaration"]
        assert len(shadow_diags) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestShadowDiagnosticScoping -v`
Expected: `test_cross_protocol_shadow_suppressed` FAILS (shadow is currently reported across protocols).

- [ ] **Step 3: Add `_protocol_from_path` helper and filter `other_nodes`**

In `ivy_lsp/lsp/diagnostics/compute.py`, add the helper function before `compute_semantic_diagnostics` (around line 265):

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

Then modify line 394 inside the D8 shadow detection block:

Replace:
```python
            other_nodes = [n for n in sym_nodes if n.file != abs_path]
```
With:
```python
            local_proto = _protocol_from_path(abs_path)
            other_nodes = [
                n for n in sym_nodes
                if n.file != abs_path
                and _protocol_from_path(n.file) == local_proto
            ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestShadowDiagnosticScoping -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/lsp/diagnostics/compute.py tests/test_diagnostic_scope.py
git commit -m "fix: scope shadow diagnostics to same protocol workspace

Shadow detection (D8) now filters other_nodes by protocol extracted
from the file path. Cross-protocol shadows are suppressed."
```

---

### Task 2: Fix unresolved include false positives (Bug 1)

**Files:**
- Modify: `ivy_lsp/lsp/diagnostics/compute.py:59-67`
- Test: `tests/test_diagnostic_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diagnostic_scope.py`:

```python
from unittest.mock import MagicMock


@pytest.mark.unit
class TestIncludeResolutionFallback:
    """Bug 1: Partitioned resolver must fall back to full resolver."""

    def test_fallback_when_partitioned_returns_none(self):
        """If resolve_partitioned returns None, fallback to resolve."""
        from ivy_lsp.lsp.diagnostics.compute import check_structural_issues

        source = "#lang ivy1.7\n\ninclude quic_types\n"
        filepath = "/ws/protocol-testing/quic/quic_stack/test.ivy"

        indexer = MagicMock()
        resolver = MagicMock()
        resolver.resolve_partitioned.return_value = None
        resolver.resolve.return_value = "/ws/protocol-testing/quic/quic_stack/quic_types.ivy"
        resolver._partition_staging = {"some_partition": ["file"]}
        indexer.resolver = resolver

        diags = check_structural_issues(source, filepath, indexer=indexer)
        unresolved = [d for d in diags if "Unresolved include" in d.message]
        assert len(unresolved) == 0

    def test_no_fallback_when_partitioned_resolves(self):
        """If resolve_partitioned succeeds, don't call fallback."""
        from ivy_lsp.lsp.diagnostics.compute import check_structural_issues

        source = "#lang ivy1.7\n\ninclude quic_types\n"
        filepath = "/ws/protocol-testing/quic/quic_stack/test.ivy"

        indexer = MagicMock()
        resolver = MagicMock()
        resolver.resolve_partitioned.return_value = "/ws/quic_types.ivy"
        resolver._partition_staging = {"some_partition": ["file"]}
        indexer.resolver = resolver

        diags = check_structural_issues(source, filepath, indexer=indexer)
        unresolved = [d for d in diags if "Unresolved include" in d.message]
        assert len(unresolved) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestIncludeResolutionFallback -v`
Expected: `test_fallback_when_partitioned_returns_none` FAILS (currently uses `resolve_partitioned` exclusively, which returns `None`).

- [ ] **Step 3: Replace callback selection with fallback wrapper**

In `ivy_lsp/lsp/diagnostics/compute.py`, replace lines 59-67:

```python
    resolve_cb = None
    if indexer:
        resolver = indexer.resolver
        # Check for real IncludeResolver with active partition staging.
        partition_staging = getattr(resolver, "_partition_staging", None)
        if isinstance(partition_staging, dict) and partition_staging:
            resolve_cb = resolver.resolve_partitioned
        else:
            resolve_cb = resolver.resolve
```

With:

```python
    resolve_cb = None
    if indexer:
        resolver = indexer.resolver
        partition_staging = getattr(resolver, "_partition_staging", None)
        if isinstance(partition_staging, dict) and partition_staging:
            _partitioned = resolver.resolve_partitioned
            _full = resolver.resolve

            def resolve_cb(name, from_file):
                result = _partitioned(name, from_file)
                if result is None:
                    result = _full(name, from_file)
                return result
        else:
            resolve_cb = resolver.resolve
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestIncludeResolutionFallback -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/lsp/diagnostics/compute.py tests/test_diagnostic_scope.py
git commit -m "fix: fall back to full resolver when partitioned returns None

During early indexing, partition staging may be incomplete. The
structural lint now tries resolve_partitioned first, then falls back
to the full resolver to avoid false positive unresolved includes."
```

---

### Task 3: Add lowercase parameter lint (Bug 4)

**Files:**
- Modify: `ivy_lsp/core/structural_lint.py`
- Modify: `ivy_lsp/core/diagnostics/codes.py`
- Modify: `ivy_lsp/lsp/diagnostics/compute.py:42-94`
- Test: `tests/test_structural_lint.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_structural_lint.py`:

```python
from ivy_lsp.core.structural_lint import check_lowercase_params


def test_lowercase_relation_param_flagged():
    source = "#lang ivy1.7\n\nrelation connected(src:cid, dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 2
    assert issues[0]["severity"] == "error"
    assert "src" in issues[0]["message"]
    assert "dst" in issues[1]["message"]


def test_uppercase_relation_param_accepted():
    source = "#lang ivy1.7\n\nrelation connected(Src:cid, Dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 0


def test_lowercase_function_param_flagged():
    source = "#lang ivy1.7\n\nfunction count(x:nat) : nat\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 1
    assert "x" in issues[0]["message"]


def test_action_lowercase_param_not_flagged():
    source = "#lang ivy1.7\n\naction send(src:cid, dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 0


def test_relation_no_params_not_flagged():
    source = "#lang ivy1.7\n\nrelation connected\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 0


def test_mixed_case_params():
    source = "#lang ivy1.7\n\nrelation link(X:node, y:node)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 1
    assert "y" in issues[0]["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_structural_lint.py::test_lowercase_relation_param_flagged -v`
Expected: FAIL with `ImportError: cannot import name 'check_lowercase_params'`

- [ ] **Step 3: Implement `check_lowercase_params` in `structural_lint.py`**

Add at the end of `ivy_lsp/core/structural_lint.py` (before the final blank line):

```python
_DECL_PARAM_RE = re.compile(
    r"^\s*(relation|function)\s+([\w.]+)\s*\(([^)]+)\)", re.MULTILINE
)


def check_lowercase_params(
    source: str,
    filepath: str,
) -> List[Dict[str, Any]]:
    """Check for lowercase-initial parameters in relation/function declarations.

    In Ivy, uppercase-initial names are logical variables (universally
    quantified). Lowercase-initial names are treated as constant references
    and will cause 'unknown symbol' errors at compile time.

    Only checks ``relation`` and ``function`` declarations. ``action``
    parameters are concrete and legitimately use lowercase names.
    """
    diags: List[Dict[str, Any]] = []

    for match in _DECL_PARAM_RE.finditer(source):
        kind = match.group(1)
        params_str = match.group(3)
        line_no = source[: match.start()].count("\n") + 1

        for param in params_str.split(","):
            param = param.strip()
            if not param:
                continue
            name = param.split(":")[0].strip()
            if not name:
                continue
            if name[0].islower():
                diags.append(
                    {
                        "line": line_no,
                        "severity": "error",
                        "message": (
                            f"Parameter '{name}' in {kind} declaration must"
                            f" start with uppercase (Ivy treats lowercase"
                            f" as constant references)"
                        ),
                        "source": "ivy-lint",
                        "code": "ivy.declaration.lowercaseParam",
                    }
                )

    return diags
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_structural_lint.py -k "lowercase" -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Register diagnostic code in `codes.py`**

Add at the end of `ivy_lsp/core/diagnostics/codes.py` (before the final closing section or at the end of the `ivy.module.*` block):

```python
# -- ivy.declaration.* -----------------------------------------------------

_reg(
    DiagnosticDescriptor(
        code="ivy.declaration.lowercaseParam",
        title="Lowercase parameter in {kind} declaration: '{name}'",
        explanation=(
            "Ivy treats lowercase-initial names as constant references, not "
            "type variables. In relation and function declarations, parameters "
            "must start with an uppercase letter to be treated as universally "
            "quantified logical variables."
        ),
        default_severity=lsp.DiagnosticSeverity.Error,
        source="ivy-lint",
    )
)
```

- [ ] **Step 6: Wire into `check_structural_issues` in `compute.py`**

In `ivy_lsp/lsp/diagnostics/compute.py`, add the import at line 48-53 (inside `check_structural_issues`):

Replace:
```python
    from ivy_lsp.core.structural_lint import (
        check_commented_out_requires,
        check_duplicate_tags,
        check_structural_issues_raw,
        check_unresolved_includes_raw,
    )
```
With:
```python
    from ivy_lsp.core.structural_lint import (
        check_commented_out_requires,
        check_duplicate_tags,
        check_lowercase_params,
        check_structural_issues_raw,
        check_unresolved_includes_raw,
    )
```

Then add after line 74 (`raw.extend(check_commented_out_requires(source, filepath))`):

```python
    raw.extend(check_lowercase_params(source, filepath))
```

- [ ] **Step 7: Run full structural lint test suite**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_structural_lint.py -v`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/core/structural_lint.py ivy_lsp/core/diagnostics/codes.py ivy_lsp/lsp/diagnostics/compute.py tests/test_structural_lint.py
git commit -m "feat: add lowercase parameter lint for relation/function declarations

Ivy treats lowercase-initial names as constant references, not logical
variables. Relation and function declarations with lowercase parameters
cause 'unknown symbol' compile errors. The new structural lint catches
this before ivy_check."
```

---

### Task 4: Filter collision count to active layers (Bug 2)

**Files:**
- Modify: `ivy_lsp/mcp/context.py:222-239`
- Test: `tests/test_diagnostic_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_diagnostic_scope.py`:

```python
@pytest.mark.unit
class TestCollisionCountFiltering:
    """Bug 2: Collision count must only count within active layers."""

    def test_cross_protocol_collisions_excluded(self):
        """Collisions between files in different protocols should not be counted."""
        from ivy_lsp.mcp.context import ToolContext

        ctx = ToolContext.__new__(ToolContext)
        ctx.workspace_root = "/ws"
        ctx.workspace_config = MagicMock()
        ctx.workspace_config.workspace_layers = []

        resolver = MagicMock()
        resolver._file_to_layer = {
            "/ws/protocol-testing/bgp/bgp_stack/types.ivy": "bgp",
            "/ws/protocol-testing/quic/quic_stack/types.ivy": "quic",
        }
        resolver._active_layers = {"bgp"}
        resolver._collision_map = {
            "types.ivy": [
                "/ws/protocol-testing/bgp/bgp_stack/types.ivy",
                "/ws/protocol-testing/quic/quic_stack/types.ivy",
            ]
        }
        ctx.include_resolver = resolver

        result = ctx.to_context_dict()
        assert result["collisions_in_scope"] == 0

    def test_same_protocol_collisions_counted(self):
        """Collisions between files in the same protocol should be counted."""
        from ivy_lsp.mcp.context import ToolContext

        ctx = ToolContext.__new__(ToolContext)
        ctx.workspace_root = "/ws"
        ctx.workspace_config = MagicMock()
        ctx.workspace_config.workspace_layers = []

        resolver = MagicMock()
        resolver._file_to_layer = {
            "/ws/protocol-testing/quic/quic_stack/types.ivy": "quic",
            "/ws/protocol-testing/quic/quic_utils/types.ivy": "quic",
        }
        resolver._active_layers = {"quic"}
        resolver._collision_map = {
            "types.ivy": [
                "/ws/protocol-testing/quic/quic_stack/types.ivy",
                "/ws/protocol-testing/quic/quic_utils/types.ivy",
            ]
        }
        ctx.include_resolver = resolver

        result = ctx.to_context_dict()
        assert result["collisions_in_scope"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestCollisionCountFiltering -v`
Expected: `test_cross_protocol_collisions_excluded` FAILS (currently counts all collisions globally).

- [ ] **Step 3: Fix collision filtering in `mcp/context.py`**

In `ivy_lsp/mcp/context.py`, the existing code at lines 222-239 already attempts filtering via `_file_to_layer` and `_active_layers`. The issue is that when files from different protocols share a basename but have different layers (e.g., `bgp` vs `quic`), the filter `ftl.get(os.path.realpath(v)) in active` catches `bgp` files when `active = {"bgp"}` but also catches `quic` files when `active = {"quic"}`. This is correct per-layer but the problem is broader: the collision map itself contains cross-protocol entries.

The real fix is to filter the collision count so that only collisions where **two or more** variants are in the active layer set are counted. Read the current code and verify the logic. The existing sum check `> 1` at line 234 should already do this, but `os.path.realpath(v)` may not match keys in `_file_to_layer` if symlinks are involved.

Replace lines 222-239:

```python
            # Filtered collision count
            cmap = getattr(resolver, "_collision_map", {})
            if cmap:
                if active:
                    in_scope_collisions = sum(
                        1
                        for variants in cmap.values()
                        if sum(
                            1
                            for v in variants
                            if ftl.get(v) in active
                            or ftl.get(os.path.realpath(v)) in active
                        )
                        > 1
                    )
                else:
                    in_scope_collisions = len(cmap)
                ctx["collisions_in_scope"] = in_scope_collisions
                ctx["collisions_total"] = len(cmap)
```

The key change: add `ftl.get(v) in active` before the `os.path.realpath(v)` check so that direct path matches work without relying on realpath resolution.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py::TestCollisionCountFiltering -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run all diagnostic-related tests**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py tests/test_structural_lint.py tests/test_collision_diagnostics.py tests/test_scoped_diagnostics.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp
git add ivy_lsp/mcp/context.py tests/test_diagnostic_scope.py
git commit -m "fix: filter collision count to active workspace layers

Collision count now only includes basenames where two or more variants
exist within the active layer set. Cross-protocol collisions are
excluded from the in_scope count."
```

---

### Task 5: Final verification

- [ ] **Step 1: Run full test suite for all touched modules**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/test_diagnostic_scope.py tests/test_structural_lint.py tests/test_collision_diagnostics.py tests/test_scoped_diagnostics.py tests/test_semantic_diagnostics.py tests/test_coverage_diagnostics.py -v`
Expected: All PASS.

- [ ] **Step 2: Verify no regressions in broader test suite**

Run: `cd panther/plugins/services/testers/panther_ivy/submodules/ivy-lsp && python -m pytest tests/ -x --timeout=30 -q 2>&1 | tail -10`
Expected: No new failures.
