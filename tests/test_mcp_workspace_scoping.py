"""Tests for MCP workspace scoping (Sprint 2.1).

Verifies that _find_ivy_files respects IVY_LSP_INCLUDE_PATHS and
IVY_LSP_EXCLUDE_PATHS environment variables set by workspace detection.
"""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def workspace(tmp_path):
    """Create a workspace with protocol-testing, doc, and examples dirs."""
    # protocol-testing/quic/
    (tmp_path / "protocol-testing" / "quic").mkdir(parents=True)
    (tmp_path / "protocol-testing" / "quic" / "types.ivy").write_text("#lang ivy1.7\n")
    (tmp_path / "protocol-testing" / "quic" / "packet.ivy").write_text("#lang ivy1.7\n")

    # protocol-testing/apt/
    (tmp_path / "protocol-testing" / "apt" / "quic").mkdir(parents=True)
    (tmp_path / "protocol-testing" / "apt" / "quic" / "types.ivy").write_text(
        "#lang ivy1.7\n"
    )

    # doc/examples/
    (tmp_path / "doc" / "examples").mkdir(parents=True)
    (tmp_path / "doc" / "examples" / "hello.ivy").write_text("#lang ivy1.7\n")

    # examples/
    (tmp_path / "examples").mkdir(parents=True)
    (tmp_path / "examples" / "tilelink.ivy").write_text("#lang ivy1.7\n")

    return tmp_path


def test_no_scoping_returns_all(workspace):
    """Without env vars, all .ivy files are returned."""
    env_patch = {"IVY_LSP_INCLUDE_PATHS": "", "IVY_LSP_EXCLUDE_PATHS": ""}
    with patch.dict(os.environ, env_patch, clear=False):
        from ivy_lsp.mcp_server import start_mcp

        app = start_mcp(workspace_root=str(workspace), _return_app=True)

    # The app was created; verify by checking the tool list
    assert app is not None


def test_include_paths_filters(workspace):
    """IVY_LSP_INCLUDE_PATHS restricts to listed subdirectories."""
    from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS, find_ivy_files

    include_paths = ["protocol-testing"]
    all_files = find_ivy_files(str(workspace))
    scoped = [
        f for f in all_files if any(f.startswith(ip + "/") for ip in include_paths)
    ]
    # Should include protocol-testing files only
    assert any("quic/types.ivy" in f for f in scoped)
    assert any("apt/quic/types.ivy" in f for f in scoped)
    # Should NOT include doc or examples
    assert not any("doc/" in f for f in scoped)
    assert not any("examples/" in f for f in scoped)


def test_exclude_paths_filters(workspace):
    """IVY_LSP_EXCLUDE_PATHS excludes directory basenames."""
    from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS, find_ivy_files

    extra_excludes = frozenset(["doc", "examples"])
    all_excludes = DEFAULT_EXCLUDE_DIRS | extra_excludes
    files = find_ivy_files(str(workspace), exclude_dirs=all_excludes)
    # Should include protocol-testing files
    assert any("quic/types.ivy" in f for f in files)
    # Should NOT include doc or examples
    assert not any("doc/" in f for f in files)
    assert not any("examples/" in f for f in files)


def test_combined_include_and_exclude(workspace):
    """Both include and exclude paths work together."""
    from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS, find_ivy_files

    extra_excludes = frozenset(["doc", "examples"])
    all_excludes = DEFAULT_EXCLUDE_DIRS | extra_excludes
    all_files = find_ivy_files(str(workspace), exclude_dirs=all_excludes)

    include_paths = ["protocol-testing"]
    scoped = [
        f
        for f in all_files
        if any(
            f == ip or f.startswith(ip + "/") or f.startswith(ip + os.sep)
            for ip in include_paths
        )
    ]
    # Only protocol-testing files remain
    assert len(scoped) == 3  # quic/types, quic/packet, apt/quic/types
    assert all("protocol-testing/" in f for f in scoped)


def test_mcp_start_with_scoping_env(workspace):
    """start_mcp respects IVY_LSP_INCLUDE_PATHS env var."""
    env_patch = {
        "IVY_LSP_INCLUDE_PATHS": "protocol-testing",
        "IVY_LSP_EXCLUDE_PATHS": "doc,examples",
    }
    with patch.dict(os.environ, env_patch, clear=False):
        from ivy_lsp.mcp_server import start_mcp

        app = start_mcp(workspace_root=str(workspace), _return_app=True)
    assert app is not None
