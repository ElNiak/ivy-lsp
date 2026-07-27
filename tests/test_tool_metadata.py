"""Tests for tool metadata completeness."""

from ivy_lsp.mcp.tools import get_tool_metadata

VALID_TIERS = {"instant", "fast", "slow", "blocking"}
VALID_RENDERING = {"hook", "raw"}
HOOK_RENDERED_TOOLS = {
    "ivy_verify",
    "ivy_compile",
    "ivy_diagnostics",
    "ivy_coverage",
    "ivy_quality",
}


class TestToolMetadataFields:
    def test_all_tools_have_tier(self):
        all_meta = get_tool_metadata()
        for name, meta in all_meta.items():
            assert "tier" in meta, f"{name} missing 'tier' field"
            assert (
                meta["tier"] in VALID_TIERS
            ), f"{name} has invalid tier: {meta['tier']}"

    def test_all_tools_have_rendering(self):
        all_meta = get_tool_metadata()
        for name, meta in all_meta.items():
            assert "rendering" in meta, f"{name} missing 'rendering' field"
            assert (
                meta["rendering"] in VALID_RENDERING
            ), f"{name} has invalid rendering: {meta['rendering']}"

    def test_hook_rendered_tools_match(self):
        all_meta = get_tool_metadata()
        actual_hook = {
            name for name, meta in all_meta.items() if meta.get("rendering") == "hook"
        }
        assert actual_hook == HOOK_RENDERED_TOOLS
