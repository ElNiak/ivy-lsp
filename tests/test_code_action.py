"""Tests for textDocument/codeAction feature."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol.types import (
    CodeActionContext,
    CodeActionKind,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
    TextDocumentIdentifier,
)

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestCodeActionImport:
    def test_import(self):
        from ivy_lsp.features.code_action import compute_code_actions

        assert compute_code_actions is not None


class TestMissingLangHeader:
    def test_quickfix_inserts_lang_header(self):
        """A diagnostic with code 'missing-lang-header' produces an insert action."""
        from ivy_lsp.features.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(0, 0), end=Position(0, 0)),
            message="Missing #lang header",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="missing-lang-header",
        )
        source = "type cid\n"
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        assert len(actions) >= 1
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "lang" in fix[0].title.lower()
        # Should have a workspace edit that inserts "#lang ivy1.7\n" at line 0
        edit = fix[0].edit
        assert edit is not None
        changes = edit.changes
        assert "file:///test.ivy" in changes
        text_edit = changes["file:///test.ivy"][0]
        assert "#lang ivy1.7" in text_edit.new_text


class TestUnresolvedInclude:
    def test_quickfix_removes_include_line(self):
        """A diagnostic with code 'unresolved-include' produces a remove action."""
        from ivy_lsp.features.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(2, 0), end=Position(2, 20)),
            message="Unresolved include: missing_file",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="unresolved-include",
        )
        source = "#lang ivy1.7\n\ninclude missing_file\ntype cid\n"
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert (
            "remove" in fix[0].title.lower()
            or "include" in fix[0].title.lower()
        )


class TestNoMatchingDiagnostic:
    def test_no_actionable_diagnostics(self):
        """Diagnostics without known codes produce no actions."""
        from ivy_lsp.features.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(0, 0), end=Position(0, 5)),
            message="Some other error",
            severity=DiagnosticSeverity.Error,
            source="ivy",
        )
        actions = compute_code_actions("file:///test.ivy", "", [diag])
        assert actions == []

    def test_empty_diagnostics(self):
        """No diagnostics produce no actions."""
        from ivy_lsp.features.code_action import compute_code_actions

        assert compute_code_actions("file:///test.ivy", "", []) == []


class TestDiagnosticCodeField:
    def test_missing_lang_header_has_code(self):
        """check_structural_issues sets code='missing-lang-header'."""
        from ivy_lsp.features.diagnostics import check_structural_issues

        source = "type cid\n"
        diags = check_structural_issues(source, "/tmp/test.ivy")
        lang_diags = [d for d in diags if d.code == "missing-lang-header"]
        assert len(lang_diags) == 1

    def test_unresolved_include_has_code(self):
        """check_structural_issues sets code='unresolved-include'."""
        from ivy_lsp.features.diagnostics import check_structural_issues

        # check_structural_issues requires an indexer whose
        # _resolver.resolve() returns None for unresolved includes.
        mock_indexer = MagicMock()
        mock_indexer._resolver.resolve.return_value = None

        source = "#lang ivy1.7\ninclude nonexistent\n"
        diags = check_structural_issues(
            source, "/tmp/test.ivy", indexer=mock_indexer
        )
        include_diags = [
            d for d in diags if d.code == "unresolved-include"
        ]
        assert len(include_diags) == 1
