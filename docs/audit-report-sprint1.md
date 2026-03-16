# Ivy-LSP MCP Tools: Sprint 1 Live Audit Report

**Date:** 2026-03-13
**Workspace:** PANTHER project (panther_ivy submodule)
**Detected by:** heuristic (PANTHER), workspace_root = panther_ivy/

---

## Executive Summary

**19 tools tested. 6 fully functional, 5 partially functional, 8 non-functional.**

Key corrections to pre-audit predictions:
1. `rfc9000_requirements.yaml` **EXISTS** (101 requirements) — the plan incorrectly said no manifests exist
2. `ivy_capabilities` reports all 3 CLI tools **available** (ivy_check, ivyc, ivy_show on PATH)
3. `ivy_impact_analysis` and `ivy_query_symbol` **DO work** — they find symbols, but with 0 edges and cross-protocol contamination
4. Visualization tools **DO work** — RequirementGraph is built lazily — but data mixes all protocols + doc/examples/

**Critical systemic issue:** Workspace scoping is broken. All tools index the entire panther_ivy directory (doc/examples/, protocol-testing/quic/, protocol-testing/apt/, examples/tilelink/, etc.) instead of scoping to a single protocol.

---

## Tool-by-Tool Results

### Category 1: Verification/Compilation (5 tools)

| Tool | Status | Notes |
|------|--------|-------|
| `ivy_capabilities` | **WORKS** | Returns `{ivy_check: true, ivyc: true, ivy_show: true}` — all tools on PATH |
| `ivy_verify` | **WORKS** | Returns structured errors. quic_types.ivy fails: `unknown symbol: zero_rtt_allowed` (expected — missing dependency context) |
| `ivy_compile` | **WORKS** | Returns structured errors. quic_server_test_stream.ivy fails: module `quic_infer.ivy` not found in module path |
| `ivy_model_info` | **WORKS** | Returns structured errors. Same `zero_rtt_allowed` error as ivy_verify |
| `ivy_lint` | **WORKS** | Fast structural checks. All protocol files pass except `new_prot_application.ivy` which correctly reports `missing-lang-header` warning |

**Verdict:** All 5 verification tools work correctly. CLI errors are legitimate (missing context/modules for isolated file checks).

#### ivy_lint defect detection matrix:

| File | Diagnostics | Notes |
|------|-------------|-------|
| quic/quic_stack/quic_types.ivy | 0 | Clean |
| quic/quic_stack/quic_frame.ivy | 0 | Clean (largest file, stress test passed) |
| bgp/bgp_stack/bgp_header_message.ivy | 0 | Clean |
| coap/coap_stack/coap_message.ivy | 0 | Clean |
| minip/minip_stack/ping_types.ivy | 0 | Clean |
| new_prot/new_prot_stack/new_prot_application.ivy | 1 warning | `missing-lang-header` — **correctly detected real defect** |

### Category 2: Dependency (1 tool)

| Tool | Status | Notes |
|------|--------|-------|
| `ivy_include_graph` | **PARTIAL** | Works but has cross-protocol contamination |

**Cross-protocol contamination evidence** (quic/quic_stack/quic_packet.ivy):
- `includes` field shows `candidates` from BOTH `protocol-testing/apt/` and `protocol-testing/quic/` for every include (e.g., `quic_types` has candidates from both workspaces)
- `included_by` lists files from BOTH apt/ and quic/ — apt files should never reference standalone quic files
- `ip` module resolves to `ivy/include/1.7-old/ip.ivy` with 3 version candidates

### Category 3: Semantic/Traceability (6 tools)

| Tool | Status | Notes |
|------|--------|-------|
| `ivy_traceability_matrix` | **PARTIAL** | Returns 101 requirements, 0 covered. Requirements loaded from `rfc9000_requirements.yaml` but no bracket-tag annotations match |
| `ivy_requirement_coverage` | **PARTIAL** | Returns coverage breakdown by level (45 MUST, 17 SHOULD, 24 MAY, 12 MUST NOT, 3 SHOULD NOT) and 13 layers. All 0% covered |
| `ivy_impact_analysis` | **PARTIAL** | Finds symbols but returns 0 edges (no edges wired). Cross-protocol: `cid` found in `examples/tilelink/` not protocol-testing |
| `ivy_query_symbol` | **PARTIAL** | Finds symbols with params. Cross-protocol: `packet_event` found in apt/quic, `cid` found in tilelink + apt/quic |
| `ivy_cross_references` | **BROKEN** | Always returns `{found: false}` — node IDs don't match any stored format |
| `ivy_extract_requirements` | **WORKS** | Correctly parses RFC text → MUST/SHOULD/MAY extraction with offset positions |

**Key issues:**
1. **Cross-protocol contamination**: `ivy_query_symbol("cid")` returns the first match from `examples/tilelink/` instead of scoping to protocol-testing/quic/
2. **0 edges**: Both `ivy_impact_analysis` and `ivy_query_symbol` find symbols but have no edges — `_build_model()` creates nodes but never wires edges
3. **0 coverage**: 101 RFC requirements in the manifest but 0 annotations match — source uses `# [1]` numeric tags, manifest uses `rfc9000:4.1` format

### Category 4: Visualization (7 tools)

| Tool | Status | Notes |
|------|--------|-------|
| `ivy_action_requirements` | **PARTIAL** | Returns 3 actions total (hello, ping, pong) — all from doc/examples/MSV/, none from protocol-testing/ |
| `ivy_model_summary` | **PARTIAL** | Returns rows but `stateVarsWritten: 2109` for ALL actions (constant = total state vars) — **bug** |
| `ivy_coverage_gaps` | **PARTIAL** | Returns 1481 unguarded state vars, 101 uncovered RFC reqs. Includes doc/examples files |
| `ivy_action_dependency_graph` | **PARTIAL** | Without state_vars: 0 edges. With state_vars: has edges but data mixes all protocols |
| `ivy_state_machine_view` | **PARTIAL** | Returns state nodes and transitions but from wrong files (apt/quic instead of quic) |
| `ivy_layered_overview` | **PARTIAL** | Returns file-based layers mixing doc/examples + all protocols |
| `ivy_smart_suggestions` | **BROKEN** | Always returns empty suggestions `[]` for any file |

**stateVarsWritten bug detail:**
Every single action shows `stateVarsWritten: 2109` regardless of which state vars the action actually writes. The value 2109 is the total number of state vars across the entire (unscoped) model. Root cause: state var write tracking is not wired per-action.

---

## Systemic Issues

### Issue 1: No Workspace Scoping (P0)

**Impact:** ALL tools except ivy_lint, ivy_verify, ivy_compile, ivy_model_info (which take explicit file paths)

The `_find_ivy_files(root)` function indexes the entire panther_ivy directory including:
- `doc/examples/MSV/` (hello, pingpong examples)
- `examples/tilelink/` (TileLink cache coherence examples)
- `protocol-testing/quic/` (standalone QUIC model)
- `protocol-testing/apt/` (APT attack model with embedded QUIC copy)
- `protocol-testing/bgp/`, `coap/`, `minip/`, `new_prot/`

Despite workspace detection setting `include_paths=["protocol-testing"]` and `exclude_paths=["submodules", "test", "doc", "examples", ...]`, MCP tools ignore these paths.

### Issue 2: Cross-Protocol Contamination (P0)

**Impact:** Include graph, symbol queries, visualization tools

APT contains a full copy of the QUIC model at `apt/apt_protocols/quic/`. Include resolution finds candidates from both copies. Symbol queries return arbitrary first-match across protocols. The include graph's `included_by` shows apt files depending on standalone quic files (impossible in real builds).

### Issue 3: stateVarsWritten Constant Bug (P1)

**Impact:** `ivy_model_summary`, `ivy_coverage_gaps`

`stateVarsWritten` shows the global count (2109) for every action instead of per-action write analysis. The `wire_state_var_edges()` pipeline is not called, so READS edges exist (from regex extraction) but WRITES edges are not computed correctly.

### Issue 4: Tag Format Mismatch (P1)

**Impact:** `ivy_traceability_matrix`, `ivy_requirement_coverage`

The YAML manifest uses `rfc9000:4.1` format IDs. The Ivy source files use `# [1]`, `# [4]` etc. (numeric, file-local). No bracket tag in the source matches any requirement ID in the manifest, resulting in 0% coverage.

### Issue 5: No Semantic Edges (P2)

**Impact:** `ivy_impact_analysis`, `ivy_cross_references`

`_build_model()` creates SymbolNode and RfcAnnotation/RfcRequirement nodes but never wires edges between them. All `incoming_edges` and `outgoing_edges` are empty.

### Issue 6: Empty Smart Suggestions (P2)

**Impact:** `ivy_smart_suggestions`

Returns `[]` for all files. The suggestion engine likely requires a fully wired RequirementGraph + SemanticModel to generate meaningful suggestions.

---

## Corrected Tool Classification

| Category | Fully Working | Partially Working | Broken |
|----------|--------------|-------------------|--------|
| Verification (5) | ivy_capabilities, ivy_verify, ivy_compile, ivy_model_info, ivy_lint | — | — |
| Dependency (1) | — | ivy_include_graph | — |
| Semantic (6) | ivy_extract_requirements | ivy_traceability_matrix, ivy_requirement_coverage, ivy_impact_analysis, ivy_query_symbol | ivy_cross_references |
| Visualization (7) | — | ivy_action_requirements, ivy_model_summary, ivy_coverage_gaps, ivy_action_dependency_graph, ivy_state_machine_view, ivy_layered_overview | ivy_smart_suggestions |

**Revised count: 6 fully working, 11 partially working (data quality issues), 2 broken**

---

## Priority Fix Order (Updated)

### P0 — Workspace Scoping
1. Make `_find_ivy_files()` respect include/exclude paths from workspace detection
2. Add optional `protocol` parameter to analysis/visualization tools
3. Prevent cross-protocol contamination in include graph and symbol queries

### P0 — Tag Format Migration
4. Begin migrating source annotations from `# [1]` to `# [rfc9000:X.Y]` format
5. Or add fallback tag resolution that maps numeric tags to manifest requirements

### P1 — stateVarsWritten Bug Fix
6. Fix `wire_state_var_edges()` pipeline in MCP mode to compute per-action writes

### P1 — Wire Semantic Edges
7. Add COVERS, CONTAINS, INCLUDES edges in `_build_model()`
8. Fix `ivy_cross_references` node ID format

### P2 — Smart Suggestions
9. Implement suggestion logic (currently returns empty)

---

## Verification Test Commands

```bash
# Validate fixes after Sprint 2:
# 1. Workspace scoping: action_requirements should return QUIC actions, not hello/pingpong
# 2. Tag coverage: traceability_matrix should show >0 covered requirements
# 3. stateVarsWritten: model_summary actions should have different stateVarsWritten values
# 4. No contamination: query_symbol("cid") should NOT return tilelink or apt results
# 5. Edges: impact_analysis("packet_event") should have >0 edges
```
