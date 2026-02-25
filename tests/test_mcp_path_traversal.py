"""Tests for MCP path traversal guard."""

import os

import pytest


def test_path_traversal_blocked(tmp_path):
    """MCP ivy_verify must reject paths that escape the workspace root."""
    from ivy_lsp.mcp_server import _validate_path

    root = str(tmp_path)
    with pytest.raises(ValueError):
        _validate_path(root, "../../etc/passwd")
    with pytest.raises(ValueError):
        _validate_path(root, "/etc/passwd")
    with pytest.raises(ValueError):
        _validate_path(root, "foo/../../etc/passwd")


def test_valid_path_accepted(tmp_path):
    """MCP must accept normal relative paths within workspace."""
    from ivy_lsp.mcp_server import _validate_path

    root = str(tmp_path)
    (tmp_path / "test.ivy").touch()
    result = _validate_path(root, "test.ivy")
    assert result == str(tmp_path / "test.ivy")


def test_root_itself_accepted(tmp_path):
    """Edge case: path resolving to root itself should be accepted."""
    from ivy_lsp.mcp_server import _validate_path

    root = str(tmp_path)
    result = _validate_path(root, ".")
    assert os.path.realpath(result) == os.path.realpath(root)


def test_subdirectory_accepted(tmp_path):
    """Paths in subdirectories should be accepted."""
    from ivy_lsp.mcp_server import _validate_path

    root = str(tmp_path)
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "model.ivy").touch()
    result = _validate_path(root, "subdir/model.ivy")
    assert result == str(sub / "model.ivy")
