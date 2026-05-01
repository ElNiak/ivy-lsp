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
    near_miss = [i for i in issues if i.get("code") == "ivy.include.nearMiss"]
    assert len(near_miss) == 1
    assert "ivy_quic_shim_client_ext_example" in near_miss[0]["message"]


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
    near_miss = [i for i in issues if i.get("code") == "ivy.include.nearMiss"]
    assert len(near_miss) == 0
    unresolved = [i for i in issues if i.get("code") == "ivy.module.unresolvedInclude"]
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
    dupes = [i for i in issues if i.get("code") == "ivy.type.duplicateTag"]
    assert len(dupes) >= 1
    assert "15" in dupes[0]["message"]


def test_placeholder_tag_flagged():
    source = (
        "#lang ivy1.7\n"
        "object unknown = {  # tag = x\n"
        "    variant this of tp = struct { val : nat }\n"
        "}\n"
    )
    issues = check_duplicate_tags(source, "/fake/tp.ivy")
    placeholders = [i for i in issues if "placeholder" in i.get("message", "").lower()]
    assert len(placeholders) == 1
    assert placeholders[0]["code"] == "ivy.type.placeholderTag"


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
    dupes = [i for i in issues if i.get("code") == "ivy.type.duplicateTag"]
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
    commented = [i for i in issues if i.get("code") == "ivy.require.commentedOut"]
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
    commented = [i for i in issues if i.get("code") == "ivy.require.commentedOut"]
    assert len(commented) == 0


def test_intentional_comment_suppressed():
    source = (
        "#lang ivy1.7\n"
        "# TODO: re-enable this\n"
        "# require x > 0;\n"
        "before foo {\n"
        "    require y > 0;\n"
        "}\n"
    )
    issues = check_commented_out_requires(source, "/fake/test.ivy")
    commented = [i for i in issues if i.get("code") == "ivy.require.commentedOut"]
    assert all(c["severity"] in ("hint", "info") for c in commented)


# --- D5: Lowercase parameter tests ---

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


# --- Task 7: param-name-style (now ivy.declaration.lowercaseParam) ---


def test_param_name_collision_lowercase_multi_char():
    source = "#lang ivy1.7\nrelation update_processed(src:bgp_id, dst:bgp_id)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" in codes


def test_param_name_single_letter_ok():
    source = "#lang ivy1.7\nrelation conn_seen(C:cid)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" not in codes


def test_param_name_function_declaration():
    source = "#lang ivy1.7\nfunction getsock(addr:ip.addr) : net.socket\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.declaration.lowercaseParam" in codes


def test_param_name_in_comment_ignored():
    source = "#lang ivy1.7\n# relation foo(src:bar)\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
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
    assert "ivy.action.unguardedAction" in codes


def test_guarded_action_ok():
    source = "#lang ivy1.7\naction send(S:cid) = {\n    require connected(S);\n    sent(S) := true;\n}\n"
    issues = check_structural_issues(source, "/fake/test.ivy")
    codes = [i.code for i in issues]
    assert "ivy.action.unguardedAction" not in codes
