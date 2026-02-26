"""Tests for ExportImportInfo data structure and light-mode extraction."""
import pytest
from ivy_lsp.analysis.test_scope import ExportImportInfo
from ivy_lsp.analysis.light_mode_extractor import extract_exports_imports_light


class TestExportImportInfoCreation:
    def test_create_empty(self):
        info = ExportImportInfo(file="/test/file.ivy")
        assert info.file == "/test/file.ivy"
        assert info.exports == []
        assert info.imports == []
        assert info.export_lines == {}
        assert info.import_lines == {}

    def test_create_with_exports(self):
        info = ExportImportInfo(
            file="/test/file.ivy",
            exports=["quic.send", "quic.recv"],
            export_lines={"quic.send": 10, "quic.recv": 15},
        )
        assert info.exports == ["quic.send", "quic.recv"]
        assert info.export_lines["quic.send"] == 10

    def test_create_with_imports(self):
        info = ExportImportInfo(
            file="/test/file.ivy",
            imports=["tls.handshake"],
            import_lines={"tls.handshake": 20},
        )
        assert info.imports == ["tls.handshake"]
        assert info.import_lines["tls.handshake"] == 20

    def test_has_exports_true(self):
        info = ExportImportInfo(
            file="/test/file.ivy",
            exports=["quic.send"],
        )
        assert info.has_exports is True

    def test_has_exports_false(self):
        info = ExportImportInfo(file="/test/file.ivy")
        assert info.has_exports is False


class TestLightModeExportExtraction:
    """Tests for regex-based export/import extraction."""

    def test_single_export(self):
        source = "export quic.send\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send"]
        assert info.export_lines == {"quic.send": 0}
        assert info.imports == []

    def test_single_import(self):
        source = "import tls.handshake\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.imports == ["tls.handshake"]
        assert info.import_lines == {"tls.handshake": 0}
        assert info.exports == []

    def test_multiple_exports(self):
        source = "export quic.send\nexport quic.recv\nexport quic.close\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send", "quic.recv", "quic.close"]
        assert info.export_lines == {"quic.send": 0, "quic.recv": 1, "quic.close": 2}

    def test_multiple_imports(self):
        source = "import tls.handshake\nimport quic.connection\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.imports == ["tls.handshake", "quic.connection"]
        assert info.import_lines == {"tls.handshake": 0, "quic.connection": 1}

    def test_exports_imports_with_surrounding_code(self):
        source = (
            "type packet\n"
            "export quic.send\n"
            "action handle(p: packet) = {\n"
            "    require p.valid;\n"
            "}\n"
            "import tls.handshake\n"
        )
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send"]
        assert info.export_lines == {"quic.send": 1}
        assert info.imports == ["tls.handshake"]
        assert info.import_lines == {"tls.handshake": 5}

    def test_empty_source(self):
        info = extract_exports_imports_light("", "/test.ivy")
        assert info.exports == []
        assert info.imports == []
        assert info.export_lines == {}
        assert info.import_lines == {}
        assert info.file == "/test.ivy"

    def test_indented_export_import(self):
        source = "    export quic.send\n    import tls.recv\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send"]
        assert info.imports == ["tls.recv"]

    def test_commented_lines_not_matched(self):
        source = (
            "# export quic.send\n"
            "export quic.recv\n"
            "# import tls.fake\n"
            "import tls.real\n"
        )
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.recv"]
        assert info.imports == ["tls.real"]

    def test_action_export_form(self):
        source = "action export quic.send\naction import tls.handshake\n"
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send"]
        assert info.export_lines == {"quic.send": 0}
        assert info.imports == ["tls.handshake"]
        assert info.import_lines == {"tls.handshake": 1}

    def test_mixed_forms(self):
        source = (
            "export quic.send\n"
            "action export quic.recv\n"
            "import tls.handshake\n"
            "action import quic.connection\n"
        )
        info = extract_exports_imports_light(source, "/test.ivy")
        assert info.exports == ["quic.send", "quic.recv"]
        assert info.imports == ["tls.handshake", "quic.connection"]
        assert info.file == "/test.ivy"


# ---------------------------------------------------------------------------
# Full-mode AST-based export/import extraction tests
# ---------------------------------------------------------------------------

from ivy_lsp.analysis.requirement_extractor import extract_exports_imports_full

FILEPATH = "/test/file.ivy"


class TestFullModeExportExtraction:
    """Tests for AST-based export/import extraction."""

    def test_returns_empty_for_none_ast(self):
        info = extract_exports_imports_full(None, FILEPATH, "")
        assert info.file == FILEPATH
        assert info.exports == []
        assert info.imports == []
        assert info.export_lines == {}
        assert info.import_lines == {}

    def test_returns_empty_for_ast_without_decls(self):
        class _NoDecls:
            pass

        info = extract_exports_imports_full(_NoDecls(), FILEPATH, "")
        assert info.exports == []
        assert info.imports == []

    def test_returns_empty_for_empty_decls(self):
        from unittest.mock import MagicMock

        ast_obj = MagicMock()
        ast_obj.decls = []
        info = extract_exports_imports_full(ast_obj, FILEPATH, "")
        assert info.exports == []
        assert info.imports == []

    def test_filepath_preserved(self):
        info = extract_exports_imports_full(None, "/custom/path.ivy", "")
        assert info.file == "/custom/path.ivy"
