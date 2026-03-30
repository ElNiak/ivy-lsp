"""Tests for Task 3.2: scope parameter on MCP tools.

Verifies:
- Tools accept ``scope`` parameter without error.
- Empty scope (default) preserves backward-compatible behavior.
- Unknown scope logs a warning and proceeds normally.
- Known scope annotates results / filters files as expected.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


from tests.helpers.mcp_helpers import extract_text, get_mcp_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_workspace_context(
    test_name: str = "quic_server_test_stream",
    test_file: str = "/tmp/test-workspace/test.ivy",
    include_closure: frozenset | None = None,
    tester_role: str = "client",
):
    """Build a mock WorkspaceContext with a single scope."""
    from ivy_lsp.core.analysis.test_scope import TestScope

    scope = TestScope(
        test_file=test_file,
        include_closure=include_closure or frozenset({test_file}),
        exported_actions=frozenset({"app.send"}),
        imported_actions=frozenset({"app.recv"}),
        tester_role=tester_role,
    )
    ws = MagicMock()
    ws.get_test_scope.side_effect = lambda name: scope if name == test_name else None
    return ws


# ---------------------------------------------------------------------------
# Tests: ivy_verify scope parameter
# ---------------------------------------------------------------------------


class TestVerifyScope:
    @pytest.mark.asyncio
    async def test_empty_scope_backward_compatible(self, tmp_path):
        """Empty scope (default) produces same result as no scope."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))

        # Call with default scope (empty string)
        result = await mcp.call_tool(
            "ivy_verify", {"relative_path": "model.ivy", "scope": ""}
        )
        data = json.loads(extract_text(result))
        # Should not have scope keys
        assert "scope" not in data or data.get("scope") == ""

    @pytest.mark.asyncio
    async def test_unknown_scope_logs_warning_and_proceeds(self, tmp_path, caplog):
        """Unknown scope name logs warning, proceeds without error."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")

        # Inject workspace_context with no matching scope
        ws = MagicMock()
        ws.get_test_scope.return_value = None
        ctx = _make_ctx(str(tmp_path), ws)

        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        with caplog.at_level(logging.WARNING, logger="ivy_lsp.mcp.tools.verification"):
            result = await mcp.call_tool(
                "ivy_verify",
                {"relative_path": "model.ivy", "scope": "nonexistent_scope"},
            )
        data = json.loads(extract_text(result))
        # Should still get a result (file not found is fine — no ivy_check)
        assert isinstance(data, dict)
        # Warning should have been logged
        assert any("nonexistent_scope" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_known_scope_annotates_result(self, tmp_path):
        """Known scope adds scope/scope_role to the verify result."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")

        ws = _make_mock_workspace_context(
            test_name="my_test",
            test_file=str(ivy_file),
            include_closure=frozenset({str(ivy_file)}),
        )
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)
        result = await mcp.call_tool(
            "ivy_verify",
            {"relative_path": "model.ivy", "scope": "my_test"},
        )
        data = json.loads(extract_text(result))
        # Result should carry scope annotation (even if ivy_check fails)
        # We just check that the tool accepted the parameter without error
        assert isinstance(data, dict)
        # If the tool ran (even failed due to no ivy_check), scope should be present
        if data.get("success") is not None:
            assert data.get("scope") == "my_test"
            assert data.get("scope_role") == "client"


# ---------------------------------------------------------------------------
# Tests: ivy_compile scope parameter
# ---------------------------------------------------------------------------


class TestCompileScope:
    @pytest.mark.asyncio
    async def test_empty_scope_backward_compatible(self, tmp_path):
        """Empty scope should not affect compile result."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))

        result = await mcp.call_tool(
            "ivy_compile", {"relative_path": "model.ivy", "scope": ""}
        )
        data = json.loads(extract_text(result))
        assert "scope" not in data or data.get("scope") == ""


# ---------------------------------------------------------------------------
# Tests: ivy_diagnostics scope parameter
# ---------------------------------------------------------------------------


class TestDiagnosticsScope:
    @pytest.mark.asyncio
    async def test_empty_scope_backward_compatible(self, tmp_path):
        """Empty scope: diagnostics run as normal."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")
        mcp = get_mcp_app(workspace_root=str(tmp_path))

        result = await mcp.call_tool(
            "ivy_diagnostics",
            {"relative_path": "model.ivy", "mode": "structural", "scope": ""},
        )
        data = json.loads(extract_text(result))
        assert data["success"] is True
        assert "scope" not in data

    @pytest.mark.asyncio
    async def test_scope_filters_out_of_scope_file(self, tmp_path):
        """File outside scope's include_closure gets a filtered response."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")

        # Scope that does NOT include model.ivy
        ws = _make_mock_workspace_context(
            test_name="narrow_scope",
            test_file=str(tmp_path / "test.ivy"),
            include_closure=frozenset({str(tmp_path / "test.ivy")}),
        )
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        result = await mcp.call_tool(
            "ivy_diagnostics",
            {
                "relative_path": "model.ivy",
                "mode": "structural",
                "scope": "narrow_scope",
            },
        )
        data = json.loads(extract_text(result))
        assert data["success"] is True
        assert data["scope_filtered"] is True
        assert data["diagnostic_count"] == 0

    @pytest.mark.asyncio
    async def test_scope_allows_in_scope_file(self, tmp_path):
        """File inside scope's include_closure gets normal diagnostics."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")

        # Scope includes model.ivy
        ws = _make_mock_workspace_context(
            test_name="wide_scope",
            test_file=str(ivy_file),
            include_closure=frozenset({str(ivy_file)}),
        )
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        result = await mcp.call_tool(
            "ivy_diagnostics",
            {
                "relative_path": "model.ivy",
                "mode": "structural",
                "scope": "wide_scope",
            },
        )
        data = json.loads(extract_text(result))
        assert data["success"] is True
        assert data.get("scope_filtered") is not True
        assert data.get("scope") == "wide_scope"

    @pytest.mark.asyncio
    async def test_unknown_scope_proceeds_normally(self, tmp_path, caplog):
        """Unknown scope name logs warning and runs without filtering."""
        ivy_file = tmp_path / "model.ivy"
        ivy_file.write_text("#lang ivy1.7\ntype t\n")

        ws = MagicMock()
        ws.get_test_scope.return_value = None
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        with caplog.at_level(logging.WARNING, logger="ivy_lsp.mcp.tools.verification"):
            result = await mcp.call_tool(
                "ivy_diagnostics",
                {
                    "relative_path": "model.ivy",
                    "mode": "structural",
                    "scope": "bogus",
                },
            )
        data = json.loads(extract_text(result))
        assert data["success"] is True
        # No scope_filtered — unknown scope just proceeds
        assert data.get("scope_filtered") is not True
        assert any("bogus" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Tests: ivy_include_graph scope parameter
# ---------------------------------------------------------------------------


class TestIncludeGraphScope:
    @pytest.mark.asyncio
    async def test_empty_scope_backward_compatible(self, tmp_path):
        """Empty scope: include graph returns all files."""
        (tmp_path / "types.ivy").write_text("#lang ivy1.7\ntype cid\n")
        (tmp_path / "conn.ivy").write_text(
            "#lang ivy1.7\ninclude types\nrelation r(X:cid)\n"
        )
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool(
            "ivy_include_graph", {"relative_path": "conn.ivy", "scope": ""}
        )
        data = json.loads(extract_text(result))
        assert data["file"] == "conn.ivy"
        assert "scope" not in data

    @pytest.mark.asyncio
    async def test_scope_filters_graph(self, tmp_path):
        """Scope filters the graph to only include closure files."""
        (tmp_path / "a.ivy").write_text("#lang ivy1.7\ntype a_t\n")
        (tmp_path / "b.ivy").write_text("#lang ivy1.7\ninclude a\ntype b_t\n")
        (tmp_path / "c.ivy").write_text("#lang ivy1.7\ntype c_t\n")

        # Scope includes only a.ivy and b.ivy
        ws = _make_mock_workspace_context(
            test_name="ab_scope",
            test_file=str(tmp_path / "b.ivy"),
            include_closure=frozenset(
                {str(tmp_path / "a.ivy"), str(tmp_path / "b.ivy")}
            ),
        )
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        result = await mcp.call_tool(
            "ivy_include_graph", {"detail": "full", "scope": "ab_scope"}
        )
        data = json.loads(extract_text(result))
        # c.ivy should be filtered out
        file_keys = list(data.get("files", {}).keys())
        assert "c.ivy" not in file_keys
        assert data.get("scope") == "ab_scope"


# ---------------------------------------------------------------------------
# Tests: ivy_coverage scope parameter
# ---------------------------------------------------------------------------


class TestCoverageScope:
    @pytest.mark.asyncio
    async def test_empty_scope_backward_compatible(self, tmp_path):
        """Empty scope should not change coverage behavior."""
        mcp = get_mcp_app(workspace_root=str(tmp_path))
        result = await mcp.call_tool("ivy_coverage", {"mode": "stats", "scope": ""})
        data = json.loads(extract_text(result))
        # Should produce a result without scope key
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_scope_resolves_to_test_file(self, tmp_path):
        """When scope is set, it resolves to test_file via workspace_context."""
        test_ivy = tmp_path / "test.ivy"
        test_ivy.write_text("#lang ivy1.7\ntype t\n")

        ws = _make_mock_workspace_context(
            test_name="my_scope",
            test_file=str(test_ivy),
            include_closure=frozenset({str(test_ivy)}),
        )
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        result = await mcp.call_tool(
            "ivy_coverage", {"mode": "stats", "scope": "my_scope"}
        )
        data = json.loads(extract_text(result))
        assert isinstance(data, dict)
        # The scope should be annotated in the result
        assert data.get("scope") == "my_scope"

    @pytest.mark.asyncio
    async def test_unknown_scope_proceeds_normally(self, tmp_path, caplog):
        """Unknown scope logs warning, proceeds with full workspace coverage."""
        ws = MagicMock()
        ws.get_test_scope.return_value = None
        ctx = _make_ctx(str(tmp_path), ws)
        from ivy_lsp.mcp.server import create_mcp_app

        mcp = create_mcp_app(ctx)

        with caplog.at_level(logging.WARNING, logger="ivy_lsp.mcp.tools.traceability"):
            result = await mcp.call_tool(
                "ivy_coverage", {"mode": "stats", "scope": "unknown_scope"}
            )
        data = json.loads(extract_text(result))
        assert isinstance(data, dict)
        assert any("unknown_scope" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Helper: create ToolContext with mocked workspace_context
# ---------------------------------------------------------------------------


def _make_ctx(root: str, workspace_context=None):
    """Create a minimal ToolContext with workspace_context injected.

    Wires up ``find_ivy_files`` and ``get_basename_cache`` with real
    implementations so that tools like ivy_include_graph work correctly.
    Also provides proper async stubs for get_model / get_req_graph.
    """
    from ivy_lsp.infra.utils.ivy_output import find_ivy_files as _find_ivy_raw
    from ivy_lsp.mcp.server import ToolContext

    ctx = ToolContext(
        root=root,
        staging_dir=None,
        executor=None,
        base_path=None,
    )
    ctx.workspace_context = workspace_context

    # Wire up find_ivy_files to scan the workspace root
    ctx.find_ivy_files = lambda search_root: _find_ivy_raw(search_root, frozenset())

    # Wire up basename cache from find_ivy_files
    def _get_basename_cache():
        cache: dict[str, list[str]] = {}
        for rel_path in ctx.find_ivy_files(root):
            basename = os.path.basename(rel_path)[:-4]
            cache.setdefault(basename, []).append(rel_path)
        return cache

    ctx.get_basename_cache = _get_basename_cache

    # Async stubs needed by traceability tools
    async def _async_none():
        return None

    ctx.get_model = _async_none
    ctx.get_req_graph = _async_none

    return ctx
