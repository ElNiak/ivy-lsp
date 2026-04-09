"""Tests for shared structural lint checks."""

from ivy_lsp.core.structural_lint import (
    check_commented_out_requires,
    check_duplicate_tags,
    check_structural_issues_raw,
    check_unresolved_includes_raw,
)


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
    unresolved = [i for i in issues if i.get("code") == "unresolved-include"]
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
