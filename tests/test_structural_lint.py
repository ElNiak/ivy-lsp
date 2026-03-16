"""Tests for shared structural lint checks."""

from ivy_lsp.utils.structural_lint import check_structural_issues_raw


def test_missing_lang_header():
    source = "type t\n"
    issues = check_structural_issues_raw(source, "/fake/test.ivy")
    msgs = [i["message"] for i in issues]
    assert any("Missing" in m and "#lang" in m for m in msgs)


def test_valid_lang_header_no_warning():
    source = "#lang ivy1.7\ntype t\n"
    issues = check_structural_issues_raw(source, "/fake/test.ivy")
    msgs = [i["message"] for i in issues]
    assert not any("#lang" in m for m in msgs)


def test_unmatched_closing_brace():
    source = "#lang ivy1.7\n}\n"
    issues = check_structural_issues_raw(source, "/fake/test.ivy")
    msgs = [i["message"] for i in issues]
    assert any("closing brace" in m.lower() for m in msgs)


def test_unmatched_opening_brace():
    source = "#lang ivy1.7\nobject foo = {\n"
    issues = check_structural_issues_raw(source, "/fake/test.ivy")
    msgs = [i["message"] for i in issues]
    assert any("opening brace" in m.lower() or "unclosed" in m.lower() for m in msgs)


def test_balanced_braces_no_issue():
    source = "#lang ivy1.7\nobject foo = {\n  type t\n}\n"
    issues = check_structural_issues_raw(source, "/fake/test.ivy")
    msgs = [i["message"] for i in issues]
    assert not any("brace" in m.lower() for m in msgs)
