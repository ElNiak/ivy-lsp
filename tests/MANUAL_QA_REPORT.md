# Manual QA Report — Ivy LSP + MCP Tools

**Date**: 2026-03-13
**Branch**: `production` (worktree: `lsp-to-claude`)

---

## Workflow 1: "Understand an unfamiliar protocol"

| Step | Tool | Result | Notes |
|------|------|--------|-------|
| 1 | `ivy_scaffold_check(protocol="quic")` | PASS | 86% completeness, 12/14 layers, 202 files. Missing: "recovery", "extensions". **Issue**: `quic_recovery/` dir exists but not detected — scaffold heuristic may need tuning. |
| 2 | `ivy_layered_overview()` | PASS | Returns per-file breakdown with actions, stateVars, requirements. **Issue**: 68KB output without scoping — confirms need for `limit` param (P2). |
| 3 | `ivy_lint(quic_types.ivy)` | PASS | Clean: 0 diagnostics. Fast structural check works. |
| 4 | `ivy_diagnostics(quic_types.ivy)` | PASS | Clean: 0 diagnostics across all 5 layers. |
| 5 | `ivy_include_graph(quic_types.ivy)` | PASS | 0 includes, 33 included_by. Graph structure correct. |
| 6 | `ivy_state_machine_view()` | OVERFLOW | **974KB output** without params. Exceeds token limit. Confirms P2 need for `limit`/pagination. With `state_var_filter` param it would be usable. |

**Verdict**: Workflow mostly works end-to-end. Two tools produce oversized output for the full QUIC workspace. Scoped calls (with `test_file` or `state_var_filter`) are required for practical use.

---

## Workflow 2: "Verify RFC coverage"

| Step | Tool | Result | Notes |
|------|------|--------|-------|
| 1 | `ivy_traceability_matrix()` | PASS | 101 requirements from RFC9000 manifest, **0 covered**. Bracket tags in .ivy files use short form `[1]`, `[4]` etc. which don't match manifest IDs `rfc9000:4.1`. This is a data/convention mismatch, not a tool bug. |
| 2 | `ivy_generate_manifest(rfc_text=..., protocol="quic")` | PASS | Extracts 3 requirements (MUST, SHOULD NOT, MAY). Valid YAML. `suggested_path` correct: `protocol-testing/quic/rfc9000_requirements.yaml`. |
| 3 | `ivy_extract_requirements(rfc_text=...)` | PASS | 4 requirements extracted. SHALL normalized to MUST NOT correctly. by_level counts accurate. |

**Verdict**: Individual tools work correctly. Coverage pipeline shows 0% because existing bracket tags (`[1]`, `[4]`) don't match the manifest format (`rfc9000:4.1`). This is an important finding for users — they need consistent tag formats.

---

## Workflow 3: "Edit and validate"

| Step | Tool | Result | Notes |
|------|------|--------|-------|
| 1 | `ivy_lint(quic_frame.ivy)` | PASS | 0 diagnostics — structural layer clean. |
| 2 | `ivy_diagnostics(quic_frame.ivy)` | PASS | **182 diagnostics**: 61 warnings (orphaned RFC tags), 121 hints (untagged assertions + unguarded state vars). Semantic + coverage layers working. |
| 3 | Diagnostic detail check | PASS | Orphaned tags correctly identified: `[4]`, `[3]`, `[1]` etc. don't match manifest. Unguarded writes flagged with `ivy.unguarded-write` code. `by_source` breakdown: `ivy-lsp-semantic: 163`, `ivy-lsp-coverage: 19`. |

**Verdict**: Edit-diagnose cycle works well. Semantic and coverage layers produce actionable diagnostics. The `file` field per diagnostic (P1 improvement) is present in the response.

---

## Workflow 4: "Navigate a symbol" (LSP)

Validated via automated tests (28 tests in `test_lsp_navigation_extended.py`):
- goToDefinition: 7 tests PASS (simple, dotted fallback, include, self-decl, nonexistent, multi-def, empty)
- findReferences: 5 tests PASS (all occurrences, exclude decl, cross-file, word boundary, empty)
- hover: 7 tests PASS (type, action+params, enum, filepath, None, relation, proximity)
- documentSymbol: 4 tests PASS (with children, no children, all kinds, empty)
- Regex patterns: 5 tests PASS (include RE, decl RE all keywords)

**Verdict**: All LSP navigation features tested and passing.

---

## Workflow 5: "Generate new protocol scaffold"

| Step | Tool | Result | Notes |
|------|------|--------|-------|
| 1 | `ivy_extract_requirements(rfc_text=...)` | PASS | See Workflow 2. |
| 2 | `ivy_generate_manifest(rfc_name="RFC9000", ...)` | PASS | Valid YAML, correct `by_level`, `suggested_path`. |
| 3 | `ivy_pattern_scaffold(protocol="myproto", pattern="serdes")` | PASS | Generated 2 files: `myproto_ser.ivy` (serializer) and `myproto_deser.ivy` (deserializer). C++ code plausible with TODO markers. Dependencies noted (`variants`). Layer 13. |

**Verdict**: Scaffold workflow produces usable starting points for new protocols.

---

## Summary of Issues Found

### Critical (blocks AI workflow)
1. **`ivy_state_machine_view` overflow**: Unscoped call on QUIC produces ~1MB — exceeds token limits. Must use `state_var_filter` or `test_file` param.
2. **`ivy_layered_overview` large output**: 68KB unscoped — needs `limit` param for practical use.

### Important (data quality)
3. **Traceability 0% coverage**: Bracket tags in existing .ivy files (`[1]`, `[4]`) don't match manifest format (`rfc9000:4.1`). Coverage pipeline shows 0% even though annotations exist.
4. **`ivy_scaffold_check` false negative**: Reports "recovery" layer missing despite `quic_recovery/` directory existing.

### Minor (output quality)
5. **`ivy_diagnostics` orphaned tag format**: Message says "Orphaned RFC tag: [4]" but doesn't suggest the expected format.
6. **`ivy_generate_manifest` empty layer field**: All requirements get `layer: ''` — the `default_layer` P2 improvement would help.

### P1 Improvements Verified
- `ivy_requirement_coverage` `uncovered_ids` field: PASS (test verified)
- `ivy_diagnostics` `file` field per diagnostic: PASS (test verified)
- `ivy_model_summary` `sort_by` + `limit` params: PASS (test verified)
- Per-level `coverage_percent` in requirement_coverage: PASS (test verified)

---

## Test Suite Status

| Suite | Tests | Status |
|-------|-------|--------|
| `test_lsp_navigation_extended.py` | 28 | All PASS |
| `test_mcp_lint_diagnostics.py` | 9 | All PASS |
| `test_mcp_traceability.py` | 8 | All PASS |
| `test_mcp_output_quality.py` | 21 | All PASS |
| **Total new tests** | **66** | **All PASS** |
| **Total suite** | **1887** | **All PASS** |
