"""Tests for shared verification functions."""

from unittest.mock import AsyncMock, patch

import pytest

from ivy_lsp.core.verification import (
    detect_isolates_for_file,
    resolve_staging_path,
    run_ivy_check,
    run_ivy_compile,
    run_ivy_show,
)
from ivy_lsp.infra.utils.async_subprocess import SubprocessResult


def test_resolve_staging_path_with_staging_dir(tmp_path):
    """When staging dir exists with symlink, returns staged path."""
    staging = tmp_path / "staging"
    staging.mkdir()
    src = tmp_path / "model.ivy"
    src.write_text("#lang ivy1.7\n")
    link = staging / "model.ivy"
    link.symlink_to(src)

    result = resolve_staging_path(str(src), staging_dir=str(staging))
    assert result == str(link)


def test_resolve_staging_path_without_staging_dir():
    """When no staging dir, returns original path."""
    result = resolve_staging_path("/tmp/model.ivy", staging_dir=None)
    assert result == "/tmp/model.ivy"


def test_resolve_staging_path_file_not_in_staging(tmp_path):
    """When staging dir exists but file not present, returns original."""
    staging = tmp_path / "staging"
    staging.mkdir()
    result = resolve_staging_path("/tmp/other.ivy", staging_dir=str(staging))
    assert result == "/tmp/other.ivy"


def test_detect_isolates_empty():
    """No symbols returns empty list."""
    assert detect_isolates_for_file(None) == []
    assert detect_isolates_for_file([]) == []


def test_detect_isolates_filters_correctly():
    """Only isolate/extract kinds are returned."""
    symbols = [
        {"name": "my_isolate", "kind": "isolate"},
        {"name": "my_extract", "kind": "extract"},
        {"name": "my_action", "kind": "action"},
        {"name": "my_type", "kind": "type"},
    ]
    result = detect_isolates_for_file(symbols)
    assert result == ["my_isolate", "my_extract"]


@pytest.mark.asyncio
async def test_run_ivy_check_success():
    """ivy_check returns structured result on success."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=["OK"],
        duration=1.5,
        returncode=0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert result["success"] is True
        assert isinstance(result["diagnostics"], list)
        assert "duration_seconds" in result
        assert "error_summary" in result
        mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_ivy_check_with_staging_dir(tmp_path):
    """ivy_check uses staging dir when provided."""
    staging = tmp_path / "staging"
    staging.mkdir()
    src = tmp_path / "model.ivy"
    src.write_text("#lang ivy1.7\n")
    link = staging / "model.ivy"
    link.symlink_to(src)

    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[],
        duration=0.5,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        await run_ivy_check(
            filepath=str(src),
            workspace_root=str(tmp_path),
            staging_dir=str(staging),
        )
        # Should have called with basename (cwd is set to staging dir)
        call_args = mock_run.call_args
        cmd = call_args[0][0]  # first positional arg
        assert cmd[-1] == "model.ivy"
        # cwd should be the staging directory
        assert call_args[1].get("cwd") == str(staging)


@pytest.mark.asyncio
async def test_run_ivy_check_with_diagnostics():
    """ivy_check parses error diagnostics correctly."""
    mock_result = SubprocessResult(
        success=False,
        message="Errors found",
        output_lines=["model.ivy:10: error: type mismatch"],
        duration=2.0,
        returncode=1,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert result["success"] is False
        assert result["diagnostic_count"] >= 1
        # Verify the parsed diagnostic content
        assert result["diagnostics"][0]["file"] == "model.ivy"
        assert result["diagnostics"][0]["line"] == 10
        assert result["diagnostics"][0]["severity"] == "error"
        assert result["diagnostics"][0]["message"] == "type mismatch"


@pytest.mark.asyncio
async def test_run_ivy_check_with_isolate():
    """ivy_check passes isolate argument correctly."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[],
        duration=1.0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
            isolate="my_isolate",
        )
        cmd = mock_run.call_args[0][0]
        assert cmd == ["ivy_check", "isolate=my_isolate", "model.ivy"]


@pytest.mark.asyncio
async def test_run_ivy_compile_success(tmp_path):
    """ivy_compile returns structured result with diagnostics."""
    (tmp_path / "model.ivy").write_text("#lang ivy1.7\n")
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=["Compiled OK"],
        duration=5.0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_compile(
            filepath=str(tmp_path / "model.ivy"),
            workspace_root=str(tmp_path),
        )
        assert result["success"] is True
        assert result["target"] == "test"
        assert "duration_seconds" in result
        assert isinstance(result["diagnostics"], list)
        assert result["diagnostic_count"] == 0
        assert "error_summary" in result
        assert (tmp_path / "build").is_dir()
        assert "raw_output" in result


@pytest.mark.asyncio
async def test_run_ivy_compile_with_isolate(tmp_path):
    """ivy_compile passes isolate and target correctly."""
    (tmp_path / "model.ivy").write_text("#lang ivy1.7\n")
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[],
        duration=3.0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_compile(
            filepath=str(tmp_path / "model.ivy"),
            workspace_root=str(tmp_path),
            target="repl",
            isolate="proto_iso",
        )
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "ivyc",
            "target=repl",
            "isolate=proto_iso",
            "model.ivy",
        ]
        assert result["target"] == "repl"


@pytest.mark.asyncio
async def test_run_ivy_show_success():
    """ivy_show returns structured result with diagnostics."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=["type foo"],
        duration=0.3,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_show(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert result["success"] is True
        assert result["raw_output"] == "type foo"
        assert "duration_seconds" in result
        assert isinstance(result["diagnostics"], list)
        assert result["diagnostic_count"] == 0
        assert "error_summary" in result
        # ivy_show should not use the semaphore
        assert mock_run.call_args[1].get("use_semaphore") is False


@pytest.mark.asyncio
async def test_run_ivy_check_success_false_when_errors_in_output():
    """Even if subprocess returns 0, success is False when errors are parsed."""
    mock_result = SubprocessResult(
        success=True,  # subprocess returned 0
        message="OK",
        output_lines=["model.ivy:5: error: undeclared variable"],
        duration=1.0,
        returncode=0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        # success should be False because diagnostics contain errors
        assert result["success"] is False
        assert result["diagnostic_count"] == 1


@pytest.mark.asyncio
async def test_run_ivy_compile_with_ivy_error_traceback(tmp_path):
    """ivy_compile extracts diagnostics from IvyError tracebacks."""
    (tmp_path / "test.ivy").write_text("#lang ivy1.7\n")
    mock_result = SubprocessResult(
        success=False,
        message="Exit code 1",
        output_lines=[
            "Traceback (most recent call last):",
            '  File "ivy_compiler.py", line 66, in other_thing',
            "    return self.clone([a.compile() for a in self.args])",
            "ivy.ivy_utils.IvyError: test.ivy: line 51: "
            "error: cannot convert argument of type milliseconds to microseconds",
        ],
        duration=2.0,
        returncode=1,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_compile(
            filepath=str(tmp_path / "test.ivy"),
            workspace_root=str(tmp_path),
        )
        assert result["success"] is False
        assert result["diagnostic_count"] >= 1
        assert result["diagnostics"][0]["file"] == "test.ivy"
        assert result["diagnostics"][0]["line"] == 51
        assert "milliseconds" in result["diagnostics"][0]["message"]
        assert result["diagnostics"][0]["source"] == "ivy_error"
        assert "test.ivy:51:" in result["error_summary"]
        # raw_output preserves the full traceback
        assert "Traceback" in result["raw_output"]


@pytest.mark.asyncio
async def test_run_ivy_compile_with_cpp_error(tmp_path):
    """ivy_compile extracts diagnostics from C++ compiler errors."""
    (tmp_path / "test.ivy").write_text("#lang ivy1.7\n")
    mock_result = SubprocessResult(
        success=False,
        message="Exit code 1",
        output_lines=[
            "/tmp/gen/test.cpp:42:10: error: undeclared identifier 'conn'",
            "/tmp/gen/test.cpp:99:5: warning: unused variable 'x'",
        ],
        duration=3.0,
        returncode=1,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_compile(
            filepath=str(tmp_path / "test.ivy"),
            workspace_root=str(tmp_path),
        )
        assert result["success"] is False
        assert result["diagnostic_count"] == 2
        errors = [d for d in result["diagnostics"] if d["severity"] == "error"]
        warnings = [d for d in result["diagnostics"] if d["severity"] == "warning"]
        assert len(errors) == 1
        assert len(warnings) == 1
        assert errors[0]["source"] == "cpp_compiler"


@pytest.mark.asyncio
async def test_run_ivy_compile_success_false_when_errors_in_output(tmp_path):
    """Even if subprocess returns 0, compile reports failure on error diagnostics."""
    (tmp_path / "model.ivy").write_text("#lang ivy1.7\n")
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[
            "ivy.ivy_utils.IvyError: model.ivy: line 10: error: type mismatch",
        ],
        duration=1.0,
        returncode=0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_compile(
            filepath=str(tmp_path / "model.ivy"),
            workspace_root=str(tmp_path),
        )
        assert result["success"] is False
        assert result["diagnostic_count"] == 1


@pytest.mark.asyncio
async def test_run_ivy_show_with_error():
    """ivy_show extracts diagnostics when it fails."""
    mock_result = SubprocessResult(
        success=False,
        message="Exit code 1",
        output_lines=[
            "ivy.ivy_utils.IvyError: model.ivy: line 3: error: unknown type",
        ],
        duration=0.5,
        returncode=1,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_show(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert result["success"] is False
        assert result["diagnostic_count"] == 1
        assert result["diagnostics"][0]["line"] == 3
        assert "unknown type" in result["error_summary"]


def test_resolve_staging_path_prefers_layer_staging(tmp_path):
    """When resolver has layer staging, prefer layer-specific dir over flat."""
    flat = tmp_path / "staging"
    flat.mkdir()
    layer_dir = tmp_path / "staging" / "layer_apt"
    layer_dir.mkdir()

    src = tmp_path / "workspace" / "apt" / "model.ivy"
    src.parent.mkdir(parents=True)
    src.write_text("#lang ivy1.7\n")

    # Flat staging has the file
    (flat / "model.ivy").symlink_to(src)
    # Layer staging also has it
    (layer_dir / "model.ivy").symlink_to(src)

    class MockResolver:
        _file_to_layer = {str(src.resolve()): "apt"}
        _partition_staging = {"apt": str(layer_dir)}

    result = resolve_staging_path(
        str(src), staging_dir=str(flat), resolver=MockResolver()
    )
    assert result == str(layer_dir / "model.ivy")


def test_resolve_staging_path_falls_back_to_flat_when_no_layer(tmp_path):
    """When resolver exists but file not in any layer, use flat staging."""
    flat = tmp_path / "staging"
    flat.mkdir()

    src = tmp_path / "model.ivy"
    src.write_text("#lang ivy1.7\n")
    (flat / "model.ivy").symlink_to(src)

    class MockResolver:
        _file_to_layer = {}
        _partition_staging = {"apt": str(tmp_path / "nonexistent")}

    result = resolve_staging_path(
        str(src), staging_dir=str(flat), resolver=MockResolver()
    )
    assert result == str(flat / "model.ivy")


def test_resolve_staging_path_no_resolver_unchanged(tmp_path):
    """When no resolver passed, behaves exactly as before."""
    flat = tmp_path / "staging"
    flat.mkdir()
    src = tmp_path / "model.ivy"
    src.write_text("#lang ivy1.7\n")
    (flat / "model.ivy").symlink_to(src)

    result = resolve_staging_path(str(src), staging_dir=str(flat), resolver=None)
    assert result == str(flat / "model.ivy")


# --- check_results integration tests ---


@pytest.mark.asyncio
async def test_run_ivy_check_returns_check_results():
    """ivy_check returns structured check_results from PASS/FAIL output."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[
            "Isolate foo.iso:",
            "",
            "    The following properties are to be checked:",
            "        order.ivy: line 4: foo.spec.prop ... PASS",
            "",
            "Isolate this:",
            "",
            "    The following program assertions are treated as guarantees:",
            "        in action bar when called from baz:",
            "            model.ivy: line 10: guarantee ... FAIL",
            "",
            "error: failed checks: 1",
        ],
        duration=5.0,
        returncode=1,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert "check_results" in result
        cr = result["check_results"]
        assert cr["total"] == 2
        assert cr["passed"] == 1
        assert cr["failed"] == 1
        assert len(cr["failed_checks"]) == 1
        assert cr["failed_checks"][0]["isolate"] == "this"


@pytest.mark.asyncio
async def test_run_ivy_check_success_false_when_checks_fail():
    """Success is False when checks fail, even if subprocess exits 0."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[
            "Isolate this:",
            "",
            "    The following program assertions are treated as guarantees:",
            "        in action foo when called from bar:",
            "            model.ivy: line 5: guarantee ... FAIL",
            "",
            "error: failed checks: 1",
        ],
        duration=1.0,
        returncode=0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert result["success"] is False
        assert result["check_results"]["failed"] == 1


@pytest.mark.asyncio
async def test_run_ivy_check_error_summary_uses_check_results():
    """error_summary reflects check failures, not just raw last line."""
    mock_result = SubprocessResult(
        success=True,
        message="OK",
        output_lines=[
            "Isolate this:",
            "",
            "    The following program assertions are treated as guarantees:",
            "        in action foo when called from bar:",
            "            model.ivy: line 5: guarantee ... FAIL",
            "            model.ivy: line 6: guarantee ... PASS",
            "",
            "error: failed checks: 1",
        ],
        duration=1.0,
        returncode=0,
    )
    with patch(
        "ivy_lsp.core.verification.run_ivy_subprocess", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = mock_result
        result = await run_ivy_check(
            filepath="/tmp/model.ivy",
            workspace_root="/tmp",
        )
        assert "1/2 checks failed" in result["error_summary"]
        assert "guarantee" in result["error_summary"]
