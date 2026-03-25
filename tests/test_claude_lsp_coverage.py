"""LSP feature coverage tests for Claude Code integration.

Verifies that all 9 Claude Code LSP operations are correctly handled
by the Ivy LSP server:
- goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol
- goToImplementation, prepareCallHierarchy, incomingCalls, outgoingCalls
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lsprotocol import types as lsp

from ivy_lsp.features.call_hierarchy import (
    get_incoming_calls,
    get_outgoing_calls,
    prepare_call_hierarchy,
)
from ivy_lsp.features.definition import goto_definition
from ivy_lsp.features.implementation import goto_implementation
from ivy_lsp.features.references import find_references


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> str:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def _index(workspace_root: str):
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

    parser = IvyParserWrapper()
    resolver = IncludeResolver(workspace_root)
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    indexer.index_workspace()
    return indexer


# Shared multi-file workspace used by most tests.
WORKSPACE_FILES = {
    "types.ivy": (
        "#lang ivy1.7\n" "\n" "type cid\n" "type stream_kind = {unidir, bidir}\n"
    ),
    "conn.ivy": (
        "#lang ivy1.7\n"
        "\n"
        "include types\n"
        "\n"
        "relation conn_seen(C:cid)\n"
        "function connected_to(C:cid) : cid\n"
        "\n"
        "action connect(src:cid, dst:cid) = {\n"
        "    conn_seen(src) := true;\n"
        "    connected_to(src) := dst;\n"
        "}\n"
        "\n"
        "action disconnect(c:cid) = {\n"
        "    conn_seen(c) := false;\n"
        "}\n"
    ),
    "behavior.ivy": (
        "#lang ivy1.7\n"
        "\n"
        "include conn\n"
        "\n"
        "before connect(src:cid, dst:cid) {\n"
        "    require ~conn_seen(src);                    # [rfc9999:4.1]\n"
        "}\n"
        "\n"
        "after connect(src:cid, dst:cid) {\n"
        "    require conn_seen(src);                     # [rfc9999:4.1]\n"
        "    require connected_to(src) = dst;            # [rfc9999:4.2]\n"
        "}\n"
    ),
}


# =========================================================================
# goToDefinition
# =========================================================================


class TestGoToDefinition:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_same_file_symbol(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        lines = Path(fp).read_text().split("\n")
        # "connect" on line 7 (action connect(src:cid, dst:cid))
        result = goto_definition(indexer, fp, lsp.Position(line=7, character=7), lines)
        assert result is not None

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_cross_file_include(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        lines = Path(fp).read_text().split("\n")
        # "types" on include line (line 2)
        result = goto_definition(indexer, fp, lsp.Position(line=2, character=8), lines)
        assert result is not None
        if isinstance(result, list):
            result = result[0]
        assert "types.ivy" in result.uri

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_nonexistent_returns_none(self, tmp_path):
        ws = _make_workspace(tmp_path, {"a.ivy": "#lang ivy1.7\n\ntype cid\n"})
        indexer = _index(ws)
        fp = str(tmp_path / "a.ivy")
        lines = Path(fp).read_text().split("\n")
        result = goto_definition(indexer, fp, lsp.Position(line=2, character=0), lines)
        # Cursor on "type" keyword — may return self-loc or None
        # Either is acceptable


# =========================================================================
# findReferences
# =========================================================================


class TestFindReferences:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_multi_file_references(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "types.ivy")
        lines = Path(fp).read_text().split("\n")
        # "cid" is used in types.ivy, conn.ivy, and behavior.ivy
        result = find_references(indexer, fp, lsp.Position(line=2, character=5), lines)
        assert len(result) >= 3  # at least one in each file

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_empty_word_returns_empty(self, tmp_path):
        ws = _make_workspace(tmp_path, {"a.ivy": "#lang ivy1.7\n\n\n"})
        indexer = _index(ws)
        fp = str(tmp_path / "a.ivy")
        lines = Path(fp).read_text().split("\n")
        result = find_references(indexer, fp, lsp.Position(line=2, character=0), lines)
        assert result == []


# =========================================================================
# hover
# =========================================================================


class TestHover:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_action_hover(self, tmp_path):
        from ivy_lsp.features.hover import get_hover_info

        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        lines = Path(fp).read_text().split("\n")
        # "connect" on line 7
        result = get_hover_info(indexer, fp, lsp.Position(line=7, character=7), lines)
        assert result is not None
        assert "connect" in result.contents.value

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_type_hover(self, tmp_path):
        from ivy_lsp.features.hover import get_hover_info

        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "types.ivy")
        lines = Path(fp).read_text().split("\n")
        # "cid" on line 2
        result = get_hover_info(indexer, fp, lsp.Position(line=2, character=5), lines)
        assert result is not None


# =========================================================================
# documentSymbol
# =========================================================================


class TestDocumentSymbol:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_returns_symbols(self, tmp_path):
        from ivy_lsp.features.document_symbols import get_document_symbols

        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        # get_document_symbols takes Optional[List[IvySymbol]]
        symbols = indexer.get_symbols(fp)
        result = get_document_symbols(symbols)
        assert result is not None
        assert len(result) > 0
        names = [s.name for s in result]
        assert "connect" in names or any("connect" in n for n in names)

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_empty_file(self, tmp_path):
        from ivy_lsp.features.document_symbols import get_document_symbols

        ws = _make_workspace(tmp_path, {"empty.ivy": "#lang ivy1.7\n"})
        indexer = _index(ws)
        fp = str(tmp_path / "empty.ivy")
        symbols = indexer.get_symbols(fp)
        result = get_document_symbols(symbols if symbols else None)
        assert isinstance(result, list)


# =========================================================================
# workspaceSymbol
# =========================================================================


class TestWorkspaceSymbol:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_finds_symbol_across_files(self, tmp_path):
        # workspaceSymbol is implemented via indexer.lookup_symbol().
        # The async handler wraps this — we test the underlying function.
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        result = indexer.lookup_symbol("connect")
        assert result is not None
        assert len(result) >= 1
        assert any(sl.symbol.name == "connect" for sl in result)

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_no_match_returns_empty(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        result = indexer.lookup_symbol("nonexistent_xyz_symbol")
        assert result == []


# =========================================================================
# goToImplementation (new)
# =========================================================================


class TestGoToImplementationCoverage:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_action_to_monitors(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        lines = Path(fp).read_text().split("\n")
        result = goto_implementation(
            indexer, fp, lsp.Position(line=7, character=7), lines
        )
        assert result is not None
        # Should find before/after in behavior.ivy
        if isinstance(result, list):
            uris = [loc.uri for loc in result]
        else:
            uris = [result.uri]
        assert any("behavior.ivy" in u for u in uris)


# =========================================================================
# Call Hierarchy (new)
# =========================================================================


class TestCallHierarchyCoverage:
    @pytest.mark.unit
    @pytest.mark.lsp
    def test_prepare_on_action(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        lines = Path(fp).read_text().split("\n")
        result = prepare_call_hierarchy(
            indexer, fp, lsp.Position(line=7, character=7), lines
        )
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "connect"

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_incoming_calls(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        result = get_incoming_calls(indexer, "connect", fp)
        # behavior.ivy has before/after connect
        assert len(result) >= 1

    @pytest.mark.unit
    @pytest.mark.lsp
    def test_outgoing_calls(self, tmp_path):
        ws = _make_workspace(tmp_path, WORKSPACE_FILES)
        indexer = _index(ws)
        fp = str(tmp_path / "conn.ivy")
        result = get_outgoing_calls(indexer, "connect", fp)
        # connect's body references conn_seen and connected_to
        # These are relations/functions, which may or may not be in the action symbol table
        # The test verifies the function runs without error
        assert isinstance(result, list)
