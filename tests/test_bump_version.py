"""Tests for the version bump script."""

import re
from pathlib import Path


class TestBumpVersionConfig:
    """Verify bump_version.py covers all version locations."""

    def test_version_files_includes_test_scaffolding(self):
        """The bump script must update the test assertion too."""
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "bump_version.py"
        spec = importlib.util.spec_from_file_location("bump_version", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        paths = [entry[0] for entry in mod.VERSION_FILES]
        assert "tests/test_task_1_1_scaffolding.py" in paths

    def test_all_version_files_patterns_match_current(self):
        """Every pattern in VERSION_FILES must match its target file."""
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "bump_version.py"
        base = script.parent.parent  # ivy_lsp project root
        spec = importlib.util.spec_from_file_location("bump_version", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        for entry in mod.VERSION_FILES:
            filepath, handler_type = entry[0], entry[1]
            if handler_type != "regex":
                continue
            pattern = entry[2]
            text = (base / filepath).read_text()
            assert re.search(
                pattern, text, re.MULTILINE
            ), f"Pattern {pattern!r} did not match in {filepath}"
