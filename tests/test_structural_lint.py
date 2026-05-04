"""Tests for shared structural lint checks."""

from ivy_lsp.core.structural_lint import (
    check_commented_out_requires,
    check_duplicate_tags,
    check_structural_issues,
    check_unresolved_includes_raw,
)


def test_missing_lang_header():
    source = "type t\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    msgs = [i.message for i in issues]
    assert any("Missing" in m and "#lang" in m for m in msgs)


def test_valid_lang_header_no_warning():
    source = "#lang ivy1.7\ntype t\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    msgs = [i.message for i in issues]
    assert not any("#lang" in m for m in msgs)


def test_unmatched_closing_brace():
    source = "#lang ivy1.7\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    msgs = [i.message for i in issues]
    assert any("closing brace" in m.lower() for m in msgs)


def test_unmatched_opening_brace():
    source = "#lang ivy1.7\nobject foo = {\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    msgs = [i.message for i in issues]
    assert any("opening brace" in m.lower() or "unclosed" in m.lower() for m in msgs)


def test_balanced_braces_no_issue():
    source = "#lang ivy1.7\nobject foo = {\n  type t\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    msgs = [i.message for i in issues]
    assert not any("brace" in m.lower() for m in msgs)


def test_near_miss_include_suggestion():
    source = "#lang ivy1.7\ninclude ivy_quic_shim_client_example_ext\n"
    known_basenames = {
        "ivy_quic_shim_client_ext_example": [
            "/fake/ivy_quic_shim_client_ext_example.ivy"
        ],
        "quic_types": ["/fake/quic_types.ivy"],
    }

    def resolver(name, from_file):
        if name in known_basenames:
            return known_basenames[name][0]
        return None

    issues = check_unresolved_includes_raw(
        source,
        "/fake/test.ivy",
        resolve_callback=resolver,
        basename_map=known_basenames,
    )
    near_miss = [i for i in issues if i.code == "ivy.include.nearMiss"]
    assert len(near_miss) == 1
    assert "ivy_quic_shim_client_ext_example" in near_miss[0].message


def test_no_near_miss_when_no_close_match():
    source = "#lang ivy1.7\ninclude completely_unknown_module\n"
    known_basenames = {
        "quic_types": ["/fake/quic_types.ivy"],
    }

    def resolver(name, from_file):
        return None

    issues = check_unresolved_includes_raw(
        source,
        "/fake/test.ivy",
        resolve_callback=resolver,
        basename_map=known_basenames,
    )
    near_miss = [i for i in issues if i.code == "ivy.include.nearMiss"]
    assert len(near_miss) == 0
    unresolved = [i for i in issues if i.code == "ivy.module.unresolvedInclude"]
    assert len(unresolved) == 1


# --- D3: Duplicate tag tests ---


def test_duplicate_tag_detected():
    source = (
        "#lang ivy1.7\n"
        "object foo = {  # tag = 15\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
        "object bar = {  # tag = 15\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    dupes = [i for i in issues if i.code == "ivy.type.duplicateTag"]
    assert len(dupes) >= 1
    assert "15" in dupes[0].message


def test_placeholder_tag_flagged():
    source = (
        "#lang ivy1.7\n"
        "object unknown = {  # tag = x\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    placeholders = [i for i in issues if "placeholder" in i.message.lower()]
    assert len(placeholders) == 1
    assert placeholders[0].code == "ivy.type.placeholderTag"


def test_unique_tags_no_issue():
    source = (
        "#lang ivy1.7\n"
        "object foo = {  # tag = 0\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
        "object bar = {  # tag = 1\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    dupes = [i for i in issues if i.code == "ivy.type.duplicateTag"]
    assert len(dupes) == 0


# --- D4: Commented-out require tests ---


def test_commented_require_detected():
    source = (
        "#lang ivy1.7\n"
        "before foo {\n"
        "    require x > 0;\n"
        "    # require y > 0;\n"
        "    # require z > 0;\n"
        "}\n"
    )
    issues = check_commented_out_requires(source, "/fake/test.ivy")
    commented = [i for i in issues if i.code == "ivy.require.commentedOut"]
    assert len(commented) == 2


def test_no_false_positive_on_regular_comment():
    source = (
        "#lang ivy1.7\n"
        "# This is a regular comment\n"
        "# See the requirements document\n"
        "before foo {\n"
        "    require x > 0;\n"
        "}\n"
    )
    issues = check_commented_out_requires(source, "/fake/test.ivy")
    commented = [i for i in issues if i.code == "ivy.require.commentedOut"]
    assert len(commented) == 0


def test_intentional_comment_suppressed():
    from lsprotocol import types as lsp

    source = (
        "#lang ivy1.7\n"
        "# TODO: re-enable this\n"
        "# require x > 0;\n"
        "before foo {\n"
        "    require y > 0;\n"
        "}\n"
    )
    issues = check_commented_out_requires(source, "/fake/test.ivy")
    commented = [i for i in issues if i.code == "ivy.require.commentedOut"]
    assert all(
        c.severity in (lsp.DiagnosticSeverity.Hint, lsp.DiagnosticSeverity.Information)
        for c in commented
    )


# --- D5: Lowercase parameter tests ---

from ivy_lsp.core.structural_lint import check_lowercase_params


def test_lowercase_relation_param_flagged():
    from lsprotocol import types as lsp

    source = "#lang ivy1.7\n\nrelation connected(src:cid, dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 2
    assert issues[0].severity == lsp.DiagnosticSeverity.Error
    assert "src" in issues[0].message
    assert "dst" in issues[1].message


def test_uppercase_relation_param_accepted():
    source = "#lang ivy1.7\n\nrelation connected(Src:cid, Dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 0


def test_lowercase_function_param_flagged():
    source = "#lang ivy1.7\n\nfunction count(x:nat) : nat\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    assert len(issues) == 1
    assert "x" in issues[0].message


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
    assert "y" in issues[0].message


# --- Task 7: param-name-style (now ivy.declaration.lowercaseParam) ---


def test_param_name_collision_lowercase_multi_char():
    source = "#lang ivy1.7\nrelation update_processed(src:bgp_id, dst:bgp_id)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" in codes


def test_param_name_single_letter_ok():
    source = "#lang ivy1.7\nrelation conn_seen(C:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" not in codes


def test_param_name_function_declaration():
    source = "#lang ivy1.7\nfunction getsock(addr:ip.addr) : net.socket\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" in codes


def test_param_name_in_comment_ignored():
    source = "#lang ivy1.7\n# relation foo(src:bar)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" not in codes


# --- Task 8: missing-init (now ivy.state.missingInit) ---


def test_missing_after_init_relation():
    source = "#lang ivy1.7\nrelation conn_seen(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.missingInit" in codes


def test_relation_with_after_init_ok():
    source = "#lang ivy1.7\nrelation conn_seen(C:cid)\nafter init {\n    conn_seen(C) := false;\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.missingInit" not in codes


def test_missing_after_init_function():
    source = "#lang ivy1.7\nfunction last_pkt(C:cid) : nat\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.missingInit" in codes


def test_type_declaration_no_init_needed():
    source = "#lang ivy1.7\ntype packet_id\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.missingInit" not in codes


# --- Task 9: empty-init, duplicate-decl, unguarded-action (canonical names) ---


def test_empty_after_init_block():
    source = "#lang ivy1.7\nrelation foo(C:cid)\nafter init {\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.emptyInit" in codes


def test_nonempty_after_init_ok():
    source = (
        "#lang ivy1.7\nrelation foo(C:cid)\nafter init {\n    foo(C) := false;\n}\n"
    )
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.state.emptyInit" not in codes


def test_duplicate_declaration_same_file():
    source = "#lang ivy1.7\nrelation foo(C:cid)\nrelation foo(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.naming.duplicateDecl" in codes


def test_no_duplicate_different_names():
    source = "#lang ivy1.7\nrelation foo(C:cid)\nrelation bar(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.naming.duplicateDecl" not in codes


def test_unguarded_action():
    source = "#lang ivy1.7\naction send(S:cid) = {\n    sent(S) := true;\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.action.unguardedWrite" in codes


def test_guarded_action_ok():
    source = "#lang ivy1.7\naction send(S:cid) = {\n    require connected(S);\n    sent(S) := true;\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.action.unguardedWrite" not in codes


# --- Phase 5 cluster B: precise-range assertions per emit site ---


def _diag_by_code(issues, code):
    """Return the first diagnostic with the given code, or None."""
    for d in issues:
        if d.code == code:
            return d
    return None


def test_range_precision_empty_init():
    source = "#lang ivy1.7\nrelation foo(C:cid)\nafter init {\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.state.emptyInit")
    assert d is not None
    assert d.line == 2
    assert d.character == 0  # "after" starts at column 0
    assert d.end_character is not None
    assert d.end_character > d.character


def test_range_precision_duplicate_decl():
    source = "#lang ivy1.7\nrelation foo(C:cid)\nrelation foo(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.naming.duplicateDecl")
    assert d is not None
    assert d.line == 2
    # Second `foo` starts at column 9 of "relation foo(C:cid)"
    assert d.character == len("relation ")
    assert d.end_character == d.character + len("foo")


def test_range_precision_unguarded_write():
    source = "#lang ivy1.7\naction send(S:cid) = {\n    sent(S) := true;\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.action.unguardedWrite")
    assert d is not None
    assert d.line == 1
    assert d.character == 0
    assert d.end_character is not None
    # Header span ends before the `{`; should be roughly len("action send(S:cid) =")
    assert d.end_character > d.character
    assert d.end_character <= len("action send(S:cid) = ")


def test_range_precision_unresolved_include():
    source = "#lang ivy1.7\ninclude completely_unknown_module\n"

    def resolver(name, from_file):
        return None

    issues = check_unresolved_includes_raw(
        source,
        "/fake/test.ivy",
        resolve_callback=resolver,
        basename_map={},
    )
    d = _diag_by_code(issues, "ivy.module.unresolvedInclude")
    assert d is not None
    assert d.line == 1
    assert d.character == len("include ")
    assert d.end_character == d.character + len("completely_unknown_module")


def test_range_precision_near_miss_include():
    source = "#lang ivy1.7\ninclude ivy_quic_shim_client_example_ext\n"
    known_basenames = {
        "ivy_quic_shim_client_ext_example": [
            "/fake/ivy_quic_shim_client_ext_example.ivy"
        ],
    }

    def resolver(name, from_file):
        return None

    issues = check_unresolved_includes_raw(
        source,
        "/fake/test.ivy",
        resolve_callback=resolver,
        basename_map=known_basenames,
    )
    d = _diag_by_code(issues, "ivy.include.nearMiss")
    assert d is not None
    assert d.line == 1
    assert d.character == len("include ")
    assert d.end_character == d.character + len("ivy_quic_shim_client_example_ext")


def test_range_precision_placeholder_tag():
    source = (
        "#lang ivy1.7\n"
        "object unknown = {  # tag = x\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    d = _diag_by_code(issues, "ivy.type.placeholderTag")
    assert d is not None
    assert d.line == 1
    # "x" is the last char of "object unknown = {  # tag = x"
    line_text = source.splitlines()[1]
    assert d.character == line_text.index("= x") + 2
    assert d.end_character == d.character + 1


def test_range_precision_duplicate_tag():
    source = (
        "#lang ivy1.7\n"
        "object foo = {  # tag = 15\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
        "object bar = {  # tag = 15\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    d = _diag_by_code(issues, "ivy.type.duplicateTag")
    assert d is not None
    assert d.line == 4  # second "tag = 15" line
    line_text = source.splitlines()[4]
    assert d.character == line_text.index("= 15") + 2
    assert d.end_character == d.character + 2


def test_range_precision_lowercase_param_single_line():
    from ivy_lsp.core.structural_lint import check_lowercase_params

    source = "#lang ivy1.7\nrelation connected(src:cid, dst:cid)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    src_d = next(i for i in issues if "src" in i.message)
    dst_d = next(i for i in issues if "dst" in i.message)
    line_text = source.splitlines()[1]
    assert src_d.line == 1
    assert src_d.character == line_text.index("src")
    assert src_d.end_character == src_d.character + 3
    assert dst_d.line == 1
    assert dst_d.character == line_text.index("dst")
    assert dst_d.end_character == dst_d.character + 3


def test_range_precision_lowercase_param_multi_line():
    """Param list spanning multiple lines: column tracking must follow newlines."""
    from ivy_lsp.core.structural_lint import check_lowercase_params

    source = "#lang ivy1.7\nrelation linked(\n    src:cid,\n    dst:cid\n)\n"
    issues = check_lowercase_params(source, "/fake/test.ivy")
    src_d = next(i for i in issues if "src" in i.message)
    dst_d = next(i for i in issues if "dst" in i.message)
    # `src` is on line 2 at column 4; `dst` is on line 3 at column 4
    assert src_d.line == 2
    assert src_d.character == 4
    assert src_d.end_character == 7
    assert dst_d.line == 3
    assert dst_d.character == 4
    assert dst_d.end_character == 7


def test_range_precision_commented_out_require():
    source = "#lang ivy1.7\n" "before foo {\n" "    # require y > 0;\n" "}\n"
    issues = check_commented_out_requires(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.require.commentedOut")
    assert d is not None
    assert d.line == 2
    # Line is "    # require y > 0;": "require" starts at column 6
    line_text = source.splitlines()[2]
    assert d.character == line_text.index("require")
    assert d.end_character == d.character + len("require")


# --- Phase 5 cluster B: line-only sites stay line-only by design ---


def test_line_only_missing_lang_header_no_regression():
    """Line-only fallback site: no token to span; diagnostic still fires."""
    source = "type t\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.syntax.missingLangHeader")
    assert d is not None
    # No precise end_character set; falls through to _DEFAULT_END_COLUMN at to_lsp().
    assert d.end_character is None


def test_line_only_unmatched_brace_no_regression():
    """Line-only fallback site: brace-depth tracking lost the token position."""
    source = "#lang ivy1.7\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.syntax.unmatchedBrace")
    assert d is not None
    assert d.end_character is None


def test_line_only_missing_init_no_regression():
    """Line-only fallback site: diagnostic flags absence of init."""
    source = "#lang ivy1.7\nrelation conn_seen(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    d = _diag_by_code(issues, "ivy.state.missingInit")
    assert d is not None
    assert d.end_character is None
