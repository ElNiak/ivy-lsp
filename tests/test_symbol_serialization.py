"""Tests for IvySymbol serialization."""
from lsprotocol.types import SymbolKind
from ivy_lsp.parsing.symbols import IvySymbol


class TestIvySymbolSerialization:
    def test_to_dict_simple(self):
        sym = IvySymbol(
            name="foo", kind=SymbolKind.Function,
            range=(0, 0, 1, 0), detail="action foo", file_path="/tmp/a.ivy",
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
            "name": "foo", "kind": int(SymbolKind.Function),
            "range": [0, 0, 1, 0], "detail": "action foo",
            "file_path": "/tmp/a.ivy", "children": [],
        }
        sym = IvySymbol.from_dict(d)
        assert sym.name == "foo"
        assert sym.kind == SymbolKind.Function
        assert sym.range == (0, 0, 1, 0)

    def test_roundtrip_with_children(self):
        child = IvySymbol(
            name="bar", kind=SymbolKind.Variable, range=(1, 4, 1, 20),
        )
        parent = IvySymbol(
            name="obj", kind=SymbolKind.Class, range=(0, 0, 5, 0),
            children=[child], file_path="/tmp/a.ivy",
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
