"""Tests for ExportImportInfo data structure."""
import pytest
from ivy_lsp.analysis.test_scope import ExportImportInfo


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
