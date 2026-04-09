"""Integration test: ivy_capabilities returns staging_health via lazy resolver."""

from unittest.mock import MagicMock


def test_ivy_capabilities_includes_staging_health_from_lazy_resolver():
    """When include_resolver is available via lazy lookup, staging_health appears."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)

    mock_resolver = MagicMock()
    mock_resolver.staging_health.return_value = {
        "total_staged": 637,
        "collisions": 2,
        "collision_basenames": ["quic_types.ivy", "order.ivy"],
        "layers_active": True,
        "layer_count": 8,
        "files_mapped_to_layers": 637,
        "symlink_failures": 0,
    }

    mock_indexer = MagicMock()
    mock_indexer.resolver = mock_resolver
    mock_server = MagicMock()
    mock_server._indexer = mock_indexer
    ctx._lsp_server_ref = mock_server

    assert ctx.include_resolver is mock_resolver
    assert hasattr(ctx.include_resolver, "staging_health")
    health = ctx.include_resolver.staging_health()
    assert health["layers_active"] is True
    assert health["layer_count"] == 8
    assert health["total_staged"] == 637


def test_ivy_capabilities_no_staging_health_when_no_resolver():
    """When no resolver is available, staging_health call should not be attempted."""
    from ivy_lsp.mcp.context import ToolContext

    ctx = ToolContext(root="/tmp", staging_dir=None, executor=None, base_path=None)
    assert ctx.include_resolver is None
