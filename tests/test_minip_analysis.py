"""Tests for analysis pipeline using the self-contained MiniP test repo.

Exercises the fallback scanner, include resolution correctness, and
requirement/monitor extraction across the multi-directory workspace.

Requires the ``ivy`` package for tokenization — tests skip when unavailable.
"""

import os
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

# Stdlib modules that may not be available in the test environment.
STDLIB_MODULES = frozenset(
    {
        "order",
        "collections",
        "c_time",
        "deserializer",
        "ip",
        "serdes",
    }
)


# ---------------------------------------------------------------------------
# Fallback scanner on minip files
# ---------------------------------------------------------------------------


@requires_ivy
class TestFallbackScannerMinip:
    """Verify the fallback scanner handles minip's diverse Ivy constructs."""

    def test_scan_ping_types(self):
        """Fallback scanner should extract types from ping_types.ivy."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        source = (MINIP_STACK_DIR / "ping_types.ivy").read_text()
        symbols, _error_info = fallback_scan(source, "ping_types.ivy")
        names = {s.name for s in symbols}
        assert "cid" in names
        assert "pkt_num" in names

    def test_scan_ping_frame_reopened_object(self):
        """Fallback scanner should handle reopened object declarations."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        source = (MINIP_STACK_DIR / "ping_frame.ivy").read_text()
        symbols, _error_info = fallback_scan(source, "ping_frame.ivy")
        names = {s.name for s in symbols}
        assert "frame" in names

    def test_scan_ping_ser_native_blocks(self):
        """Fallback scanner should tolerate C++ native blocks in ping_ser.ivy."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        source = (MINIP_STACK_DIR / "ping_ser.ivy").read_text()
        # Should not raise — native blocks are skipped
        symbols, _error_info = fallback_scan(source, "ping_ser.ivy")
        assert isinstance(symbols, list)

    def test_scan_ping_endpoint(self):
        """Fallback scanner should extract module definitions from ping_endpoint.ivy."""
        from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

        source = (MINIP_DIR / "minip_entities" / "ping_endpoint.ivy").read_text()
        symbols, _error_info = fallback_scan(source, "ping_endpoint.ivy")
        names = {s.name for s in symbols}
        # Should find client_ep or server_ep module
        assert len(names) > 0, "Expected symbols from ping_endpoint.ivy"


# ---------------------------------------------------------------------------
# Include resolution completeness
# ---------------------------------------------------------------------------


class TestIncludeResolutionMinip:
    """Verify include resolution correctness for the minip workspace."""

    def test_unresolved_are_only_stdlib(self, minip_indexer):
        """Any unresolved includes should be from the known stdlib set."""
        all_files = minip_indexer.get_all_ivy_file_paths()
        unexpected_unresolved = []
        for filepath in all_files:
            source = Path(filepath).read_text()
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("include "):
                    mod_name = stripped.split()[1]
                    resolved = minip_indexer.resolver.resolve(mod_name, filepath)
                    if resolved is None and mod_name not in STDLIB_MODULES:
                        unexpected_unresolved.append(
                            f"{os.path.basename(filepath)}: {mod_name}"
                        )
        assert (
            not unexpected_unresolved
        ), f"Unexpected unresolved includes: {unexpected_unresolved}"

    def test_staging_directory_has_all_files(self, minip_indexer):
        """The staging directory should contain symlinks for all workspace files."""
        resolver = minip_indexer.resolver
        assert resolver._staging_dir is not None, "Staging should be active"
        staged_count = len(resolver._staged_files)
        # Should have staged all 19 .ivy files (or close to it, depending on
        # which files are discovered — test/ exclusion may reduce count)
        assert staged_count >= 15, f"Expected >=15 staged files, got {staged_count}"


# ---------------------------------------------------------------------------
# Requirement / monitor extraction
# ---------------------------------------------------------------------------


@requires_ivy
class TestRequirementExtractionMinip:
    """Verify requirement and monitor extraction from minip sources."""

    def test_extract_requirements_from_ping_frame(self):
        """ping_frame.ivy has require statements that should be extracted."""
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_requirements_light,
        )

        source = (MINIP_STACK_DIR / "ping_frame.ivy").read_text()
        filepath = str(MINIP_STACK_DIR / "ping_frame.ivy")
        reqs, _writes = extract_requirements_light(source, filepath)
        assert isinstance(reqs, list)
        assert len(reqs) > 0, "ping_frame.ivy should have require/ensure statements"

    def test_extract_requirements_from_ping_application(self):
        """ping_application.ivy has a require statement in a before block."""
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_requirements_light,
        )

        source = (MINIP_STACK_DIR / "ping_application.ivy").read_text()
        filepath = str(MINIP_STACK_DIR / "ping_application.ivy")
        reqs, _writes = extract_requirements_light(source, filepath)
        assert isinstance(reqs, list)
        assert len(reqs) > 0, "ping_application.ivy should have require statements"

    def test_extract_exports_imports_from_shim(self):
        """ping_shim.ivy should have export/import info."""
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_exports_imports_light,
        )

        source = (MINIP_DIR / "minip_shims" / "ping_shim.ivy").read_text()
        filepath = str(MINIP_DIR / "minip_shims" / "ping_shim.ivy")
        info = extract_exports_imports_light(source, filepath)
        assert info is not None
