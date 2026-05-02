"""Integration tests: full LSP schema compliance for the diagnostic pipeline.

Drives the pipeline against a deliberately broken .ivy fixture and asserts
every published Diagnostic carries the LSP 3.17 schema fields.

This is the wire-level fence: while test_no_raw_dict_diagnostics.py
enforces emit-time discipline (every producer constructs IvyDiagnostic),
this test verifies the OUTPUT shape — that fields aren't stripped or
dropped anywhere along the pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

import pytest
from lsprotocol import types as lsp

from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

pytestmark = pytest.mark.unit

_FIXTURE = Path(__file__).parent / "fixtures" / "schema_compliance_sample.ivy"

_VALID_SEVERITIES = frozenset(
    (
        lsp.DiagnosticSeverity.Error,
        lsp.DiagnosticSeverity.Warning,
        lsp.DiagnosticSeverity.Information,
        lsp.DiagnosticSeverity.Hint,
    )
)


def _drive_pipeline(filepath: Path) -> List[lsp.Diagnostic]:
    """Run the structural diagnostic pipeline on *filepath*.

    Mirrors the call shape used by test_task_3_2_diagnostics.py: parser=None
    triggers structural-only checks which cover check_structural_issues()
    and its sub-checks without requiring a live IvyParserWrapper.
    """
    source = filepath.read_text()
    return compute_diagnostics(
        parser=None,
        source=source,
        filepath=str(filepath),
    )


def _drive_full_pipeline(filepath: Path) -> List[lsp.Diagnostic]:
    """Drive compute_diagnostics with a real parser, indexer, and semantic model.

    Builds a single-file workspace at *filepath*'s parent, indexes it,
    seeds a SemanticModel with one orphan-tag annotation, and invokes
    compute_diagnostics with all five cluster paths active.

    Path normalization note: macOS resolves $TMPDIR through ``/private/tmp``
    while ``Path.absolute()`` preserves the ``/tmp`` prefix. The indexer
    stores file paths in their realpath form, so the *filepath* passed to
    compute_diagnostics — and the file field on any seeded SemanticModel
    nodes — must use os.path.realpath. Otherwise the cluster filters
    (e.g. coverage_hints' ``action_node.file != filepath`` check) drop
    every diagnostic and the test silently undercovers.

    Skips when the ``ivy`` package isn't installed (required by the
    parser); mirrors the behavior of the ``minip_indexer`` fixture in
    conftest.py.
    """
    try:
        import ivy  # noqa: F401
    except ImportError:
        pytest.skip("ivy package not installed (required for parser)")

    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.parsing.parser_session import IvyParserWrapper
    from ivy_lsp.core.semantic.model import SemanticModel
    from ivy_lsp.core.semantic.nodes import RfcAnnotation, RfcRequirement

    workspace = filepath.parent
    abs_path = os.path.realpath(str(filepath))
    source = filepath.read_text()

    parser = IvyParserWrapper()
    resolver = IncludeResolver(str(workspace))
    indexer = WorkspaceIndexer(str(workspace), parser, resolver)
    indexer.index_workspace()

    # Seed the SemanticModel with an annotation pointing at the bracket-tagged
    # require line (orphan tag — the requirement we register has a different ID).
    # The orphan-tag check at compute_semantic_diagnostics filters annotations
    # by file == abs_path, so the file field MUST match the realpath.
    model = SemanticModel()
    lines = source.splitlines()
    orphan_line = next(
        (i for i, ln in enumerate(lines) if "rfc9000:99.99" in ln),
        None,
    )
    if orphan_line is not None:
        model.add_node(
            RfcAnnotation(
                id=f"{abs_path}:{orphan_line}:0",
                file=abs_path,
                line=orphan_line,
                tags=["rfc9000:99.99"],
            )
        )
    model.add_node(
        RfcRequirement(
            id="rfc9000:4.1",
            rfc="RFC9000",
            section="4.1",
            text="...",
            level="MUST",
        )
    )

    parse_result = parser.parse(source, abs_path)
    return compute_diagnostics(
        parser=parser,
        source=source,
        filepath=abs_path,
        indexer=indexer,
        semantic_model=model,
        parse_result=parse_result,
    )


def _assert_full_lsp_schema(diagnostics: List[lsp.Diagnostic]) -> None:
    """Fail with a per-diagnostic gap report if any LSP 3.17 field is missing.

    Fields checked: message (non-empty), code (non-empty), severity (valid
    enum value), source (non-empty), range (not None), and
    code_description.href (non-empty, Phase 1 design requirement).
    """
    violations: List[tuple[str, str, List[str]]] = []
    for d in diagnostics:
        missing_fields: List[str] = []

        if not (d.message and d.message.strip()):
            missing_fields.append("message")
        if not d.code:
            missing_fields.append("code")
        if d.severity not in _VALID_SEVERITIES:
            missing_fields.append("severity")
        if not d.source:
            missing_fields.append("source")
        if d.range is None:
            missing_fields.append("range")
        if d.code_description is None or not d.code_description.href:
            missing_fields.append("code_description.href")

        if missing_fields:
            code_label = str(d.code) if d.code else "<no code>"
            source_label = str(d.source) if d.source else "<no source>"
            violations.append((code_label, source_label, missing_fields))

    if violations:
        lines = ["LSP schema gaps detected (code | source | missing fields):"]
        for code_label, source_label, missing in violations:
            lines.append(f"  [{code_label}] source={source_label!r} missing={missing}")
        pytest.fail("\n".join(lines))


def test_fixture_exists():
    """Guard: fixture file must be present before any pipeline test runs."""
    assert _FIXTURE.exists(), f"Fixture missing: {_FIXTURE}"


def test_published_diagnostics_have_full_lsp_schema(tmp_path):
    """Every Diagnostic from the broken fixture must carry all 6 LSP 3.17 fields.

    Drives the structural-only path (parser=None) — covers the structural-lint
    cluster only. Wire-level coverage of the remaining four clusters is
    handled by test_all_clusters_have_lsp_schema_fields.

    If code_description is missing this test fails and reports the exact
    (code, source) pairs where the gap exists — that is the intended
    deliverable for Phase 1 gap analysis.
    """
    target = tmp_path / "schema_compliance_sample.ivy"
    target.write_text(_FIXTURE.read_text())

    diagnostics = _drive_pipeline(target)

    assert diagnostics, (
        "Fixture is supposed to produce diagnostics but none were returned. "
        "Check that check_structural_issues() runs for the broken fixture."
    )

    _assert_full_lsp_schema(diagnostics)


def test_at_least_one_code_description_present(tmp_path):
    """Sanity: at least one diagnostic in the pipeline does carry code_description.

    This verifies that IvyDiagnostic.to_lsp() wiring works end-to-end for
    diagnostics that go through the IR (not just raw lsp.Diagnostic paths).
    If this fails, code_description injection in rich_diagnostic.py is broken.
    """
    target = tmp_path / "schema_compliance_sample.ivy"
    target.write_text(_FIXTURE.read_text())

    diagnostics = _drive_pipeline(target)

    with_code_description = [
        d
        for d in diagnostics
        if d.code_description is not None and bool(d.code_description.href)
    ]
    assert with_code_description, (
        "No diagnostic in the pipeline carries code_description. "
        "IvyDiagnostic.to_lsp() code_description injection may be broken."
    )


def test_all_clusters_have_lsp_schema_fields(tmp_path):
    """Wire-level fence across all five clusters in compute_diagnostics.

    The parser=None path only exercises check_structural_issues. The other
    four cluster paths — requirement, semantic, coverage, pattern — only
    fire when a real parser, indexer, or semantic_model is supplied. This
    test drives the full pipeline so every cluster is on the wire and
    asserts each diagnostic carries the 6 LSP 3.17 schema fields.

    Cluster discriminators (positive predicates, derived from compute.py
    flow + the diagnostic registry):

    | Cluster        | Discriminator                                  |
    |----------------|------------------------------------------------|
    | structural     | source == "ivy-lint"                           |
    | requirement    | code  == "ivy.invariant.highImpactVar"         |
    | semantic       | source == "ivy-lsp-semantic"                   |
    | coverage       | DiagnosticTag.Unnecessary in tags              |
    | pattern        | code  == "ivy.action.missingFinalize"          |

    The pattern check requires a basename containing "test"; the test
    writes the fixture to `test_schema_compliance_sample.ivy` so the
    pattern path engages. The fixture itself triggers all five clusters
    when fed through a populated WorkspaceIndexer + SemanticModel.
    """
    # Pattern check engages only when basename contains "test"; the fixture
    # filename in tests/fixtures/ deliberately omits that prefix so the
    # parser=None tests above don't see pattern diagnostics. Write under a
    # test_-prefixed name here.
    target = tmp_path / "test_schema_compliance_sample.ivy"
    target.write_text(_FIXTURE.read_text())

    diagnostics = _drive_full_pipeline(target)

    assert diagnostics, (
        "Full-pipeline drive returned no diagnostics. The fixture is "
        "supposed to trigger every cluster — check that the indexer, "
        "parser, and semantic_model wiring is intact."
    )

    _assert_full_lsp_schema(diagnostics)

    # Cluster coverage assertions: each cluster contributes at least one diag.
    structural = [d for d in diagnostics if d.source == "ivy-lint"]
    requirement = [d for d in diagnostics if d.code == "ivy.invariant.highImpactVar"]
    semantic = [d for d in diagnostics if d.source == "ivy-lsp-semantic"]
    coverage = [
        d
        for d in diagnostics
        if d.tags is not None and lsp.DiagnosticTag.Unnecessary in d.tags
    ]
    pattern = [d for d in diagnostics if d.code == "ivy.action.missingFinalize"]

    missing_clusters: List[str] = []
    if not structural:
        missing_clusters.append("structural (no diagnostic with source='ivy-lint')")
    if not requirement:
        missing_clusters.append(
            "requirement (no diagnostic with code='ivy.invariant.highImpactVar')"
        )
    if not semantic:
        missing_clusters.append(
            "semantic (no diagnostic with source='ivy-lsp-semantic')"
        )
    if not coverage:
        missing_clusters.append(
            "coverage (no diagnostic carrying DiagnosticTag.Unnecessary)"
        )
    if not pattern:
        missing_clusters.append(
            "pattern (no diagnostic with code='ivy.action.missingFinalize')"
        )

    if missing_clusters:
        observed = sorted({(str(d.code), d.source) for d in diagnostics})
        pytest.fail(
            "Missing cluster coverage:\n  "
            + "\n  ".join(missing_clusters)
            + "\n\nObserved (code, source) pairs:\n  "
            + "\n  ".join(f"{c} | {s}" for c, s in observed)
        )
