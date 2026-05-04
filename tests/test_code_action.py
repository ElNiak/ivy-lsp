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


class TestUnmatchedBraceQuickFix:
    def test_closing_brace_removes_offending_token(self):
        """Quickfix for "Unmatched closing brace" deletes the offending `}`."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        # Line 1 = "}"; the `}` at col 0 is unmatched.
        source = "#lang ivy1.7\n}\n"
        diag = Diagnostic(
            range=Range(start=Position(1, 0), end=Position(1, 0)),
            message="Unmatched closing brace",
            severity=DiagnosticSeverity.Error,
            source="ivy-lint",
            code="ivy.syntax.unmatchedBrace",
        )
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "remove" in fix[0].title.lower()
        edits = fix[0].edit.changes["file:///test.ivy"]
        assert len(edits) == 1
        edit = edits[0]
        # Edit deletes 1 character at the offending `}` position.
        assert edit.range.start.line == 1
        assert edit.range.start.character == 0
        assert edit.range.end.character == 1
        assert edit.new_text == ""

    def test_opening_brace_appends_closers_at_eof(self):
        """Quickfix for "Unmatched opening brace (N unclosed)" appends N `}` at EOF."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        source = "#lang ivy1.7\nobject foo = {\nobject bar = {\n"
        diag = Diagnostic(
            range=Range(start=Position(2, 0), end=Position(2, 0)),
            message="Unmatched opening brace (2 unclosed)",
            severity=DiagnosticSeverity.Error,
            source="ivy-lint",
            code="ivy.syntax.unmatchedBrace",
        )
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "closing brace" in fix[0].title.lower()
        edits = fix[0].edit.changes["file:///test.ivy"]
        assert len(edits) == 1
        edit = edits[0]
        # Insert at end of file.
        lines = source.split("\n")
        last_line = max(0, len(lines) - 1)
        assert edit.range.start.line == last_line
        # Snippet contains exactly 2 closing braces.
        assert edit.new_text.count("}") == 2


class TestMissingFinalizeQuickFix:
    def test_appends_finalize_skeleton_at_eof(self):
        """Quickfix appends an `export action _finalize` skeleton at end of file."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        source = "#lang ivy1.7\nexport action send(x:t)\n"
        diag = Diagnostic(
            range=Range(start=Position(0, 0), end=Position(0, 0)),
            message="Test file has exports but no _finalize action.",
            severity=DiagnosticSeverity.Warning,
            source="ivy-semantic",
            code="ivy.action.missingFinalize",
        )
        actions = compute_code_actions("file:///x_test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "_finalize" in fix[0].title.lower() or "finalize" in fix[0].title.lower()
        edits = fix[0].edit.changes["file:///x_test.ivy"]
        assert len(edits) == 1
        edit = edits[0]
        # Insert at end of file (zero-width).
        lines = source.split("\n")
        last_line = max(0, len(lines) - 1)
        assert edit.range.start.line == last_line
        assert edit.range.end == edit.range.start
        # Skeleton content
        assert "export action _finalize" in edit.new_text


class TestRfcMissingTagQuickFix:
    def test_appends_template_at_assertion_end(self):
        """Quickfix appends a bracket-tag template at the assertion's end_character."""
        from ivy_lsp.lsp.code_action import compute_code_actions

        # Source line 2 = "  require x > 0;"; assertion ends at col 16.
        source = "#lang ivy1.7\n\n  require x > 0;\n"
        diag = Diagnostic(
            # diag.range spans the assertion: col 2 = "require" .. col 16 = end of `;`.
            range=Range(start=Position(2, 2), end=Position(2, 16)),
            message="Assertion without RFC bracket tag annotation",
            severity=DiagnosticSeverity.Hint,
            source="ivy-rfc",
            code="ivy.rfc.missingBracketTag",
        )
        actions = compute_code_actions("file:///test.ivy", source, [diag])
        fix = [a for a in actions if a.kind == CodeActionKind.QuickFix]
        assert len(fix) == 1
        assert "rfc" in fix[0].title.lower() or "tag" in fix[0].title.lower()
        edits = fix[0].edit.changes["file:///test.ivy"]
        assert len(edits) == 1
        edit = edits[0]
        # Insert at end of assertion (zero-width range at line 2 col 16).
        assert edit.range.start.line == 2
        assert edit.range.start.character == 16
        assert edit.range.end == edit.range.start
        # Template content
        assert "rfc" in edit.new_text.lower()
        assert "[" in edit.new_text and "]" in edit.new_text
