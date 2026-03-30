"""Tests for parser/compilation using the self-contained MiniP test repo.

Exercises the fallback scanner and PLY parser against minip's diverse
Ivy constructs: reopened objects, C++ native blocks, ellipsis syntax.

Requires the ``ivy`` package — tests skip when unavailable.
"""

import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from tests.conftest import MINIP_DIR, MINIP_STACK_DIR

try:
    import ivy  # noqa: F401

    IVY_AVAILABLE = True
except ImportError:
    IVY_AVAILABLE = False

requires_ivy = pytest.mark.skipif(not IVY_AVAILABLE, reason="ivy package not installed")


# ---------------------------------------------------------------------------
# Fallback scanner parsing tests
# ---------------------------------------------------------------------------


@requires_ivy
class TestFallbackParserMinip:
    """Test that the fallback scanner handles all minip files without crashing."""

    def test_all_stack_files_scan_without_error(self):
        """Every .ivy file in minip_stack/ should scan without raising."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        for ivy_file in sorted(MINIP_STACK_DIR.glob("*.ivy")):
            source = ivy_file.read_text()
            symbols, _error_info = fallback_scan(source, ivy_file.name)
            assert isinstance(
                symbols, list
            ), f"fallback_scan failed for {ivy_file.name}"

    def test_all_entity_files_scan(self):
        """Entity and behavior files should scan without raising."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        for subdir in ("minip_entities", "minip_entities_behavior", "minip_shims"):
            dir_path = MINIP_DIR / subdir
            if not dir_path.exists():
                continue
            for ivy_file in sorted(dir_path.glob("*.ivy")):
                source = ivy_file.read_text()
                symbols, _error_info = fallback_scan(source, ivy_file.name)
                assert isinstance(
                    symbols, list
                ), f"fallback_scan failed for {subdir}/{ivy_file.name}"

    def test_native_block_tolerance(self):
        """ping_ser.ivy and ping_deser.ivy contain <<< impl/member C++ blocks."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        for name in ("ping_ser.ivy", "ping_deser.ivy"):
            source = (MINIP_STACK_DIR / name).read_text()
            assert "<<<" in source, f"{name} should contain <<< blocks"
            symbols, _error_info = fallback_scan(source, name)
            assert isinstance(
                symbols, list
            ), f"fallback_scan crashed on native blocks in {name}"

    def test_reopened_object_produces_symbols(self):
        """ping_frame.ivy reopens object frame 4 times — should produce symbols."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        source = (MINIP_STACK_DIR / "ping_frame.ivy").read_text()
        # Count how many times "object frame" appears
        frame_count = source.count("object frame")
        assert (
            frame_count >= 2
        ), f"Expected >=2 'object frame' declarations, got {frame_count}"
        symbols, _error_info = fallback_scan(source, "ping_frame.ivy")
        names = {s.name for s in symbols}
        assert "frame" in names, "Should extract 'frame' from reopened object"


# ---------------------------------------------------------------------------
# PLY parser tests (require ivy package)
# ---------------------------------------------------------------------------


@requires_ivy
@pytest.mark.slow
class TestPlyParserMinip:
    """Test the full PLY parser on minip files. Requires the ivy package."""

    def test_ply_parse_ping_types(self):
        """PLY parser should handle ping_types.ivy."""
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = (MINIP_STACK_DIR / "ping_types.ivy").read_text()
        result = parser.parse(source, str(MINIP_STACK_DIR / "ping_types.ivy"))
        # Even if there are parse warnings, the result should exist
        assert result is not None
        # AST should be populated (success may be False for partial parse)
        assert result.ast is not None or not result.errors

    def test_ply_parse_ping_frame(self):
        """PLY parser should handle reopened objects in ping_frame.ivy."""
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = (MINIP_STACK_DIR / "ping_frame.ivy").read_text()
        result = parser.parse(source, str(MINIP_STACK_DIR / "ping_frame.ivy"))
        assert result is not None
