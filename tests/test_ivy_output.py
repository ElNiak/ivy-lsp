"""Tests for shared ivy_check output parser."""
from ivy_lsp.utils.ivy_output import parse_ivy_check_lines, find_ivy_files


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
