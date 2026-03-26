"""Tests for IvySymbol serialization."""

from lsprotocol.types import SymbolKind

from ivy_lsp.core.parsing.symbols import IvySymbol


class TestIvySymbolSerialization:
    def test_to_dict_simple(self):
        sym = IvySymbol(
            name="foo",
            kind=SymbolKind.Function,
            range=(0, 0, 1, 0),
            detail="action foo",
            file_path="/tmp/a.ivy",
        )
        d = sym.to_dict()
        assert d["name"] == "foo"
        assert d["kind"] == int(SymbolKind.Function)
        assert d["range"] == [0, 0, 1, 0]
        assert d["detail"] == "action foo"
        assert d["file_path"] == "/tmp/a.ivy"
        assert d["children"] == []

    def test_from_dict_simple(self):
        d = {
            "name": "foo",
            "kind": int(SymbolKind.Function),
            "range": [0, 0, 1, 0],
            "detail": "action foo",
            "file_path": "/tmp/a.ivy",
            "children": [],
            "synthetic": False,
        }
        sym = IvySymbol.from_dict(d)
        assert sym.name == "foo"
        assert sym.kind == SymbolKind.Function
        assert sym.range == (0, 0, 1, 0)

    def test_roundtrip_with_children(self):
        child = IvySymbol(
            name="bar",
            kind=SymbolKind.Variable,
            range=(1, 4, 1, 20),
        )
        parent = IvySymbol(
            name="obj",
            kind=SymbolKind.Class,
            range=(0, 0, 5, 0),
            children=[child],
            file_path="/tmp/a.ivy",
        )
        restored = IvySymbol.from_dict(parent.to_dict())
        assert restored.name == "obj"
        assert len(restored.children) == 1
        assert restored.children[0].name == "bar"
        assert restored.children[0].kind == SymbolKind.Variable

    def test_roundtrip_preserves_none_detail(self):
        sym = IvySymbol(name="x", kind=SymbolKind.Variable, range=(0, 0, 0, 5))
        restored = IvySymbol.from_dict(sym.to_dict())
        assert restored.detail is None
        assert restored.file_path is None


from ivy_lsp.core.analysis.test_scope import ExportImportInfo


class TestExportImportInfoSerialization:
    def test_to_dict(self):
        info = ExportImportInfo(
            file="/tmp/test.ivy",
            exports=["foo", "bar"],
            imports=["baz"],
            export_lines={"foo": 10, "bar": 20},
            import_lines={"baz": 30},
        )
        d = info.to_dict()
        assert d["file"] == "/tmp/test.ivy"
        assert d["exports"] == ["foo", "bar"]
        assert d["export_lines"] == {"foo": 10, "bar": 20}

    def test_from_dict(self):
        d = {
            "file": "/tmp/test.ivy",
            "exports": ["foo"],
            "imports": [],
            "export_lines": {"foo": 10},
            "import_lines": {},
        }
        info = ExportImportInfo.from_dict(d)
        assert info.file == "/tmp/test.ivy"
        assert info.exports == ["foo"]
        assert info.has_exports is True

    def test_roundtrip(self):
        info = ExportImportInfo(
            file="/tmp/a.ivy",
            exports=["x"],
            imports=["y"],
            export_lines={"x": 1},
            import_lines={"y": 2},
        )
        restored = ExportImportInfo.from_dict(info.to_dict())
        assert restored.file == info.file
        assert restored.exports == info.exports
        assert restored.imports == info.imports
