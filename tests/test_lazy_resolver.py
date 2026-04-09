"""Test that ToolContext.include_resolver lazily resolves from the LSP server."""

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest


def test_include_resolver_direct_assignment():
    """Direct assignment via setter should be returned by the getter."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)
    resolver = MagicMock()
    ctx.include_resolver = resolver
    assert ctx.include_resolver is resolver


def test_include_resolver_defaults_to_none():
    """Without assignment or server ref, include_resolver should be None."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)
    assert ctx.include_resolver is None


def test_include_resolver_lazy_from_server_ref():
    """When _lsp_server_ref is set and indexer has a resolver, return it lazily."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)

    mock_resolver = MagicMock()
    mock_indexer = MagicMock()
    mock_indexer.resolver = mock_resolver
    mock_server = MagicMock()
    mock_server._indexer = mock_indexer

    ctx._lsp_server_ref = mock_server
    assert ctx.include_resolver is mock_resolver


def test_include_resolver_lazy_none_when_indexer_not_ready():
    """When server ref is set but indexer is None, return None."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)

    mock_server = MagicMock()
    mock_server._indexer = None

    ctx._lsp_server_ref = mock_server
    assert ctx.include_resolver is None


def test_include_resolver_direct_takes_precedence_over_lazy():
    """Direct assignment should take precedence over lazy resolution."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)

    direct_resolver = MagicMock(name="direct")
    lazy_resolver = MagicMock(name="lazy")
    mock_indexer = MagicMock()
    mock_indexer.resolver = lazy_resolver
    mock_server = MagicMock()
    mock_server._indexer = mock_indexer

    ctx._lsp_server_ref = mock_server
    ctx.include_resolver = direct_resolver

    assert ctx.include_resolver is direct_resolver


def test_include_resolver_lazy_picks_up_late_indexer():
    """If indexer initializes after context creation, lazy resolution picks it up."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)

    mock_server = MagicMock()
    mock_server._indexer = None
    ctx._lsp_server_ref = mock_server

    assert ctx.include_resolver is None

    late_resolver = MagicMock(name="late")
    mock_indexer = MagicMock()
    mock_indexer.resolver = late_resolver
    mock_server._indexer = mock_indexer

    assert ctx.include_resolver is late_resolver


def test_from_lsp_server_stores_server_ref():
    """from_lsp_server should store the server as _lsp_server_ref."""
    from ivy_lsp.mcp.context import ToolContext

    mock_server = MagicMock()
    mock_server._indexer = None
    mock_server._semantic_model = None
    mock_server._initializing = False

    ctx = ToolContext.from_lsp_server(mock_server)
    assert ctx._lsp_server_ref is mock_server


def test_from_lsp_server_lazy_resolver_with_late_indexer():
    """from_lsp_server context should pick up resolver from a late-initializing indexer."""
    from ivy_lsp.mcp.context import ToolContext

    mock_server = MagicMock()
    mock_server._indexer = None
    mock_server._semantic_model = None
    mock_server._initializing = False

    ctx = ToolContext.from_lsp_server(mock_server)

    assert ctx.include_resolver is None

    late_resolver = MagicMock(name="late_resolver")
    late_resolver._staging_dir = "/tmp/staging"
    late_resolver._exclude_paths = []
    mock_indexer = MagicMock()
    mock_indexer.resolver = late_resolver
    mock_server._indexer = mock_indexer

    assert ctx.include_resolver is late_resolver
