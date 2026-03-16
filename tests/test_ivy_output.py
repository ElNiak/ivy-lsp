"""Tests for shared ivy_check output parser and unified error extraction."""

from ivy_lsp.utils.ivy_output import (
    extract_error_summary,
    find_ivy_files,
    format_ivy_error,
    format_ivy_errors,
    parse_ivy_check_lines,
    parse_ivy_output,
)


def test_parse_error_line():
    output = "test.ivy:10: error: type mismatch"
    result = parse_ivy_check_lines(output)
    assert len(result) == 1
    assert result[0] == {
        "file": "test.ivy",
        "line": 10,
        "severity": "error",
        "message": "type mismatch",
    }


def test_parse_warning_line():
    output = "model.ivy:42: warning: unused variable"
    result = parse_ivy_check_lines(output)
    assert result[0]["severity"] == "warning"
    assert result[0]["line"] == 42


def test_parse_multiline_output():
    output = (
        "Checking test.ivy...\n"
        "test.ivy:5: error: undeclared\n"
        "test.ivy:12: warning: shadowed\n"
        "OK\n"
    )
    result = parse_ivy_check_lines(output)
    assert len(result) == 2


def test_parse_empty_output():
    assert parse_ivy_check_lines("") == []


def test_parse_file_with_colons_in_path():
    output = "C:\\Users\\dev\\test.ivy:5: error: bad type"
    result = parse_ivy_check_lines(output)
    assert len(result) == 1
    assert result[0]["file"] == "C:\\Users\\dev\\test.ivy"
    assert result[0]["line"] == 5


def test_find_ivy_files(tmp_path):
    (tmp_path / "a.ivy").touch()
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "b.ivy").touch()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "c.ivy").touch()

    found = find_ivy_files(str(tmp_path))
    assert "a.ivy" in found
    assert any("b.ivy" in f for f in found)
    assert not any(".git" in f for f in found)
    assert found == sorted(found)


# --- parse_ivy_output tests ---


def test_parse_ivy_output_standard_format():
    """Standard ivy_check format is parsed with source='ivy_check'."""
    output = "test.ivy:10: error: type mismatch"
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0] == {
        "file": "test.ivy",
        "line": 10,
        "severity": "error",
        "message": "type mismatch",
        "source": "ivy_check",
    }


def test_parse_ivy_output_ivy_error_traceback():
    """IvyError traceback format is extracted with source='ivy_error'."""
    output = (
        "ivy.ivy_utils.IvyError: "
        "/tmp/quic_client_test_tp_error.ivy: line 51: "
        "error: cannot convert argument of type milliseconds to microseconds"
    )
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0]["file"] == "/tmp/quic_client_test_tp_error.ivy"
    assert result[0]["line"] == 51
    assert result[0]["severity"] == "error"
    assert "milliseconds to microseconds" in result[0]["message"]
    assert result[0]["source"] == "ivy_error"


def test_parse_ivy_output_ivy_error_without_severity():
    """IvyError without explicit severity keyword defaults to 'error'."""
    output = "ivy.ivy_utils.IvyError: model.ivy: line 10: " "undeclared: variable x"
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0]["severity"] == "error"
    assert result[0]["message"] == "undeclared: variable x"


def test_parse_ivy_output_cpp_compiler_error():
    """C++ compiler error format is parsed with source='cpp_compiler'."""
    output = "/tmp/ivy_gen/test.cpp:42:10: error: undeclared identifier 'conn'"
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0]["file"] == "/tmp/ivy_gen/test.cpp"
    assert result[0]["line"] == 42
    assert result[0]["severity"] == "error"
    assert "undeclared identifier" in result[0]["message"]
    assert result[0]["source"] == "cpp_compiler"


def test_parse_ivy_output_cpp_fatal_error():
    """C++ 'fatal error' is normalized to 'error'."""
    output = "/usr/include/missing.h:1:10: fatal error: file not found"
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0]["severity"] == "error"


def test_parse_ivy_output_mixed_formats():
    """Multiple error formats in one output are all captured."""
    output = (
        "Traceback (most recent call last):\n"
        "  File ivy_compiler.py line 52\n"
        "ivy.ivy_utils.IvyError: model.ivy: line 10: error: type mismatch\n"
        "  some other traceback line\n"
        "/tmp/gen.cpp:99:5: warning: unused variable 'x'\n"
    )
    result = parse_ivy_output(output)
    assert len(result) == 2
    sources = {d["source"] for d in result}
    assert "ivy_error" in sources
    assert "cpp_compiler" in sources


def test_parse_ivy_output_deduplicates():
    """Same file+line+message from different formats is reported once."""
    output = (
        "model.ivy:10: error: type mismatch\n"
        "ivy.ivy_utils.IvyError: model.ivy: line 10: error: type mismatch\n"
    )
    result = parse_ivy_output(output)
    # Should not have duplicates
    assert len(result) == 1


def test_parse_ivy_output_empty():
    """Empty output returns empty list."""
    assert parse_ivy_output("") == []


def test_parse_ivy_output_no_errors():
    """Output with no error lines returns empty list."""
    output = "Checking model.ivy...\nOK\n"
    assert parse_ivy_output(output) == []


def test_parse_ivy_output_full_traceback():
    """Real-world full traceback extracts the IvyError at the bottom."""
    output = (
        "Traceback (most recent call last):\n"
        '  File "ivy_compiler.py", line 66, in other_thing\n'
        "    return self.clone([a.compile() for a in self.args])\n"
        '  File "ivy_compiler.py", line 52, in thing\n'
        "    return self.cmpl()\n"
        '  File "ivy_compiler.py", line 220, in sort_infer_covariant\n'
        '    raise IvyError(None,"cannot convert...")\n'
        "ivy.ivy_utils.IvyError: "
        "/var/folders/tmp/quic_client_test_tp_error.ivy: line 51: "
        "error: cannot convert argument of type milliseconds to microseconds"
    )
    result = parse_ivy_output(output)
    assert len(result) == 1
    assert result[0]["source"] == "ivy_error"
    assert result[0]["line"] == 51
    assert "milliseconds to microseconds" in result[0]["message"]


# --- extract_error_summary tests ---


def test_extract_error_summary_from_diagnostics():
    """Summary is formatted from first error diagnostic."""
    diagnostics = [
        {
            "file": "model.ivy",
            "line": 42,
            "severity": "error",
            "message": "type mismatch",
        },
    ]
    summary = extract_error_summary("", diagnostics)
    assert summary == "model.ivy:42: type mismatch"


def test_extract_error_summary_prefers_errors_over_warnings():
    """First error is used even if warnings come first."""
    diagnostics = [
        {"file": "a.ivy", "line": 1, "severity": "warning", "message": "unused"},
        {"file": "b.ivy", "line": 5, "severity": "error", "message": "bad type"},
    ]
    summary = extract_error_summary("", diagnostics)
    assert summary == "b.ivy:5: bad type"


def test_extract_error_summary_warning_fallback():
    """If only warnings, uses first warning."""
    diagnostics = [
        {"file": "a.ivy", "line": 1, "severity": "warning", "message": "unused"},
    ]
    summary = extract_error_summary("", diagnostics)
    assert summary == "a.ivy:1: unused"


def test_extract_error_summary_fallback_to_last_line():
    """Without diagnostics, uses last non-empty line of raw output."""
    raw = "some info\nTimed out after 120s\n\n"
    summary = extract_error_summary(raw, [])
    assert summary == "Timed out after 120s"


def test_extract_error_summary_empty():
    """Empty output and no diagnostics returns empty string."""
    assert extract_error_summary("", []) == ""
    assert extract_error_summary("", None) == ""


# --- format_ivy_error tests ---


def test_format_ivy_error_duplicate_tuple():
    """Two-location tuple formats as duplicate."""
    err = ("prot", ("/path/to/quic_shim.ivy", 47), ("/path/to/quic_shim_ext.ivy", 47))
    result = format_ivy_error(err)
    assert "prot" in result
    assert "quic_shim.ivy:47" in result
    assert "quic_shim_ext.ivy:47" in result
    assert "Duplicate" in result


def test_format_ivy_error_nested_include():
    """Nested include chain is flattened to readable format."""
    err = (
        "prot.idx",
        ("/path/shim.ivy", 47, ("/path/protection.ivy", 13)),
        ("/path/shim_ext.ivy", 47, ("/path/protection.ivy", 13)),
    )
    result = format_ivy_error(err)
    assert "prot.idx" in result
    assert "shim.ivy:47" in result
    assert "protection.ivy:13" in result
    assert "->" in result  # include chain arrow


def test_format_ivy_error_deeply_nested():
    """Deeply nested include chain is fully flattened."""
    err = (
        "prot.idx.spec.transitivity",
        (
            "/path/shim.ivy",
            47,
            (
                "/path/protection.ivy",
                13,
                (
                    "/path/order.ivy",
                    30,
                    ("/path/order.ivy", 10, ("/path/order.ivy", 4)),
                ),
            ),
        ),
        None,
    )
    result = format_ivy_error(err)
    assert "prot.idx.spec.transitivity" in result
    assert "order.ivy:4" in result
    assert "order.ivy:30" in result


def test_format_ivy_error_unresolved():
    """All-None locations format as unresolved."""
    err = ("prot.idx.t", None, None)
    result = format_ivy_error(err)
    assert "prot.idx.t" in result
    assert "Unresolved" in result


def test_format_ivy_error_single_location():
    """Single non-None location formats as conflict."""
    err = ("sym", ("/path/file.ivy", 10))
    result = format_ivy_error(err)
    assert "sym" in result
    assert "file.ivy:10" in result
    assert "Conflict" in result


def test_format_ivy_error_with_msg_attribute():
    """Error objects with .msg attribute use the message directly."""

    class FakeError:
        msg = "type mismatch"

    result = format_ivy_error(FakeError())
    assert result == "type mismatch"


def test_format_ivy_error_plain_string():
    """Plain strings pass through unchanged."""
    assert format_ivy_error("some error text") == "some error text"


def test_format_ivy_error_exception():
    """Generic exceptions use str() representation."""
    err = ValueError("bad value")
    result = format_ivy_error(err)
    assert "bad value" in result


# --- format_ivy_errors tests ---


def test_format_ivy_errors_empty():
    """Empty error list returns '(none)'."""
    assert format_ivy_errors([]) == "(none)"


def test_format_ivy_errors_small_list():
    """Small list (<= 10) formats each error individually."""
    errors = [
        ("a", ("/f1.ivy", 1), ("/f2.ivy", 2)),
        ("b", None, None),
    ]
    result = format_ivy_errors(errors)
    assert "Duplicate" in result
    assert "Unresolved" in result
    assert "a" in result
    assert "b" in result


def test_format_ivy_errors_large_list_groups_by_category():
    """Large list (> 10) groups errors by category with counts."""
    errors = []
    for i in range(8):
        errors.append((f"dup_{i}", (f"/f1.ivy", i), (f"/f2.ivy", i)))
    for i in range(5):
        errors.append((f"unresolved_{i}", None, None))
    result = format_ivy_errors(errors)
    assert "13 errors" in result
    assert "8 duplicate symbols" in result
    assert "5 unresolved" in result
    assert "dup_0" in result
    assert "unresolved_0" in result


def test_format_ivy_errors_large_list_truncates_samples():
    """Sample names are truncated to 5 entries."""
    errors = []
    for i in range(20):
        errors.append((f"sym_{i}", (f"/f1.ivy", i), (f"/f2.ivy", i)))
    result = format_ivy_errors(errors)
    assert "20 errors" in result
    assert "..." in result
    assert "sym_0" in result
    assert "sym_4" in result
    # sym_5 and beyond should not appear in samples
    assert "sym_5" not in result
