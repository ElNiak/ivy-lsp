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

    def test_variant_type_frame(self):
        """Frame has 3 variants: ping(tag=0), pong(tag=1), timestamp(tag=2)."""
        result = find_variants_impl("frame", MINIP_DIR)
        assert result["type_name"] == "frame"
        assert result["kind"] == "variant"
        assert result["file"].endswith("ping_frame.ivy")
        assert len(result["members"]) == 3

        ping = result["members"][0]
        assert ping["name"] == "ping"
        assert ping["tag"] == 0
        assert ping["wire_type"] == "0x01"
        assert ping["fields"][0]["name"] == "data"

        pong = result["members"][1]
        assert pong["name"] == "pong"
        assert pong["tag"] == 1
        assert pong["wire_type"] == "0x02"

        ts = result["members"][2]
        assert ts["name"] == "timestamp"
        assert ts["tag"] == 2
        assert ts["wire_type"] == "0x03"
        assert ts["fields"][0]["name"] == "time"

    def test_tag_ordering_cross_check(self):
        """Tag integers match open_tag() dispatch order in serializer."""
        result = find_variants_impl("frame", MINIP_DIR)
        for i, member in enumerate(result["members"]):
            assert (
                member["tag"] == i
            ), f"Tag mismatch for {member['name']}: expected {i}, got {member['tag']}"

    def test_type_not_found_returns_error(self):
        """Unknown type name returns an error dict without crashing."""
        result = find_variants_impl("nonexistent_type", MINIP_DIR)
        assert "error" in result
        assert result["type_name"] == "nonexistent_type"
