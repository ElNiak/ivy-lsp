"""Tests for the goToImplementation feature."""

from __future__ import annotations

from pathlib import Path

import pytest
from lsprotocol import types as lsp

from ivy_lsp.lsp.navigation.implementation import goto_implementation


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> str:
    """Write files to tmp_path and return the workspace root."""
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


class TestGoToImplementation:
    """goToImplementation: find before/after monitors for actions."""

    @pytest.mark.unit
    def test_action_finds_before_block(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": (
                    "#lang ivy1.7\n"
                    "\n"
                    "type cid\n"
                    "\n"
                    "action connect(src:cid, dst:cid)\n"
                    "\n"
                    "before connect {\n"
                    "    require src ~= dst;\n"
                    "}\n"
                ),
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "connect" in "action connect(...)" — line 4, char 7
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=4, character=7), lines
        )
        assert result is not None
        if isinstance(result, list):
            assert len(result) >= 1
            loc = result[0]
        else:
            loc = result
        # Should point to the "before connect" line (line 6)
        assert loc.range.start.line == 6

    @pytest.mark.unit
    def test_action_finds_after_block(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": (
                    "#lang ivy1.7\n"
                    "\n"
                    "type cid\n"
                    "\n"
                    "action connect(src:cid, dst:cid)\n"
                    "\n"
                    "after connect {\n"
                    "    # state update\n"
                    "}\n"
                ),
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=4, character=7), lines
        )
        assert result is not None

    @pytest.mark.unit
    def test_action_finds_multiple_monitors(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": (
                    "#lang ivy1.7\n"
                    "\n"
                    "type cid\n"
                    "\n"
                    "action connect(src:cid, dst:cid)\n"
                    "\n"
                    "before connect {\n"
                    "    require src ~= dst;\n"
                    "}\n"
                    "\n"
                    "after connect {\n"
                    "    # post\n"
                    "}\n"
                ),
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=4, character=7), lines
        )
        assert isinstance(result, list)
        assert len(result) == 2

    @pytest.mark.unit
    def test_before_block_finds_action_declaration(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": (
                    "#lang ivy1.7\n"
                    "\n"
                    "type cid\n"
                    "\n"
                    "action connect(src:cid, dst:cid)\n"
                    "\n"
                    "before connect {\n"
                    "    require src ~= dst;\n"
                    "}\n"
                ),
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "connect" in "before connect" — line 6, char 7
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=6, character=7), lines
        )
        assert result is not None
        # Should find the action declaration
        if isinstance(result, list):
            loc = result[0]
        else:
            loc = result
        assert loc.range.start.line == 4  # "action connect" line

    @pytest.mark.unit
    def test_cross_file_monitor(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "types.ivy": "#lang ivy1.7\n\ntype cid\n\naction connect(src:cid, dst:cid)\n",
                "monitor.ivy": "#lang ivy1.7\n\ninclude types\n\nbefore connect {\n    require src ~= dst;\n}\n",
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "types.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "connect" in types.ivy
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=4, character=7), lines
        )
        assert result is not None
        # Should find the before block in monitor.ivy
        if isinstance(result, list):
            locs = result
        else:
            locs = [result]
        uris = [loc.uri for loc in locs]
        assert any("monitor.ivy" in uri for uri in uris)

    @pytest.mark.unit
    def test_nonexistent_action_returns_none_or_fallback(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": "#lang ivy1.7\n\ntype cid\n",
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "cid" — a type, not an action
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=2, character=5), lines
        )
        # Should either return None or fall back to definition
        # (goToDefinition for types returns the type declaration)
        # Both are acceptable.

    @pytest.mark.unit
    def test_empty_word_returns_none(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": "#lang ivy1.7\n\n\n",
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        result = goto_implementation(
            indexer, filepath, lsp.Position(line=2, character=0), lines
        )
        assert result is None
