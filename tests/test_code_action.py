"""Tests for textDocument/codeAction feature."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

from lsprotocol.types import (
    CodeActionKind,
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestCodeActionImport:
    def test_import(self):
        from ivy_lsp.lsp.code_action import compute_code_actions

        assert compute_code_actions is not None


class TestMissingLangHeader:
    def test_quickfix_inserts_lang_header(self):
        """A diagnostic with code 'ivy.syntax.missingLangHeader' produces an insert action."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(0, 0), end=Position(0, 0)),
            message="Missing #lang header",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="ivy.syntax.missingLangHeader",
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
        assert changes is not None
        assert "file:///test.ivy" in changes
        text_edit = changes["file:///test.ivy"][0]
        assert "#lang ivy1.7" in text_edit.new_text


class TestUnresolvedInclude:
    def test_quickfix_removes_include_line(self):
        """A diagnostic with code 'ivy.module.unresolvedInclude' produces a remove action."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(2, 0), end=Position(2, 20)),
            message="Unresolved include: missing_file",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="ivy.module.unresolvedInclude",
        )
        source = "#lang ivy1.7\n\ninclude missing_file\ntype cid\n"
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "remove" in fix[0].title.lower() or "include" in fix[0].title.lower()

    def test_quickfix_last_line_include_no_trailing_newline(self):
        """Include on last line without trailing newline produces valid range."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(1, 0), end=Position(1, 20)),
            message="Unresolved include: missing",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="ivy.module.unresolvedInclude",
        )
        source = "#lang ivy1.7\ninclude missing"  # No trailing newline
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert fix[0].edit is not None
        _changes = fix[0].edit.changes
        assert _changes is not None
        edit = _changes["file:///test.ivy"][0]
        # end_line should not exceed the last line index (1)
        assert edit.range.end.line <= 1


class TestNoMatchingDiagnostic:
    def test_no_actionable_diagnostics(self):
        """Diagnostics without known codes produce no actions."""
        from ivy_lsp.lsp.code_action import compute_code_actions

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
        from ivy_lsp.lsp.code_action import compute_code_actions

        assert compute_code_actions("file:///test.ivy", "", []) == []

    def test_out_of_bounds_diagnostic_line(self):
        """Diagnostic with line beyond source produces no action."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        diag = Diagnostic(
            range=Range(start=Position(99, 0), end=Position(99, 20)),
            message="Unresolved include",
            severity=DiagnosticSeverity.Warning,
            source="ivy-lsp",
            code="ivy.module.unresolvedInclude",
        )
        source = "#lang ivy1.7\ntype cid\n"
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        assert actions == []


class TestDiagnosticCodeField:
    def test_missing_lang_header_has_code(self):
        """check_structural_issues sets code='ivy.syntax.missingLangHeader'."""
        from ivy_lsp.lsp.diagnostics.compute import check_structural_issues

        source = "type cid\n"
        diags = check_structural_issues(source, "/tmp/test.ivy")
        lang_diags = [d for d in diags if d.code == "ivy.syntax.missingLangHeader"]
        assert len(lang_diags) == 1

    def test_unresolved_include_has_code(self):
        """check_structural_issues sets code='ivy.module.unresolvedInclude'."""
        from ivy_lsp.lsp.diagnostics.compute import check_structural_issues

        # check_structural_issues requires an indexer whose
        # _resolver.resolve() returns None for unresolved includes.
        mock_indexer = MagicMock()
        mock_indexer.resolver.resolve.return_value = None

        source = "#lang ivy1.7\ninclude nonexistent\n"
        diags = check_structural_issues(source, "/tmp/test.ivy", indexer=mock_indexer)
        include_diags = [d for d in diags if d.code == "ivy.module.unresolvedInclude"]
        assert len(include_diags) == 1
