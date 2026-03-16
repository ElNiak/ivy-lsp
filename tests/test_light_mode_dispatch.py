"""Test that light_mode_extractor dispatches to lexer when available."""

from ivy_lsp.analysis.light_mode_extractor import extract_requirements_light

FILEPATH = "test.ivy"


class TestDispatch:
    def test_light_returns_correct_results(self):
        """Basic smoke: dispatch produces correct results regardless of path."""
        source = "before foo.step {\n    require x > 0;\n}\n"
        reqs, writes = extract_requirements_light(source, FILEPATH)
        assert len(reqs) == 1
        assert reqs[0].kind == "require"
        assert reqs[0].monitor_action == "foo.step"

    def test_regex_fallback_works(self):
        """Force regex path and verify it still works."""
        import ivy_lsp.analysis.light_mode_extractor as mod

        saved = mod._LEXER_AVAILABLE
        try:
            mod._LEXER_AVAILABLE = False
            source = "before foo.step {\n    require x > 0;\n}\n"
            reqs, writes = extract_requirements_light(source, FILEPATH)
            assert len(reqs) == 1
            assert reqs[0].kind == "require"
        finally:
            mod._LEXER_AVAILABLE = saved

    def test_lexer_path_used_when_available(self):
        """Verify lexer path is active (PLY lexer is available in test env)."""
        import ivy_lsp.analysis.light_mode_extractor as mod

        assert mod._LEXER_AVAILABLE is True

    def test_exports_dispatch(self):
        """Export extraction works through dispatch."""
        from ivy_lsp.analysis.light_mode_extractor import extract_exports_imports_light

        source = "export foo\nimport bar\n"
        info = extract_exports_imports_light(source, FILEPATH)
        assert "foo" in info.exports
        assert "bar" in info.imports

    def test_exports_regex_fallback(self):
        """Export extraction regex fallback works."""
        import ivy_lsp.analysis.light_mode_extractor as mod
        from ivy_lsp.analysis.light_mode_extractor import extract_exports_imports_light

        saved = mod._LEXER_AVAILABLE
        try:
            mod._LEXER_AVAILABLE = False
            source = "export foo\nimport bar\n"
            info = extract_exports_imports_light(source, FILEPATH)
            assert "foo" in info.exports
            assert "bar" in info.imports
        finally:
            mod._LEXER_AVAILABLE = saved
