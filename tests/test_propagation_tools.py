"""Tests for propagation analysis MCP tools."""

import os

import pytest

from ivy_lsp.mcp.tools.propagation import find_variants_impl

MINIP_DIR = os.environ.get(
    "PANTHER_IVY_PROTOCOL_DIR",
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "protocol-testing", "minip"
    ),
)


class TestFindVariants:
    def test_struct_type_ping_packet(self):
        """ping_packet is a struct with one field: payload : frame.arr."""
        result = find_variants_impl("ping_packet", MINIP_DIR)
        assert result["type_name"] == "ping_packet"
        assert result["kind"] == "struct"
        assert result["file"].endswith("ping_packet.ivy")
        assert len(result["fields"]) == 1
        f = result["fields"][0]
        assert f["name"] == "payload"
        assert f["type"] == "frame.arr"
        assert f["is_array"] is True

    def test_type_not_found_returns_error(self):
        """Unknown type name returns an error dict without crashing."""
        result = find_variants_impl("nonexistent_type", MINIP_DIR)
        assert "error" in result
        assert result["type_name"] == "nonexistent_type"
