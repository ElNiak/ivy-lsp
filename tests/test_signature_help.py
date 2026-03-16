"""Tests for textDocument/signatureHelp feature."""

import sys
from pathlib import Path

import pytest
from lsprotocol.types import Position

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestSignatureHelpImport:
    def test_import(self):
        from ivy_lsp.features.signature_help import compute_signature_help

        assert compute_signature_help is not None


class TestParseParams:
    def test_parse_simple_params(self):
        """Parse '(x:t, y:t)' into parameter list."""
        from ivy_lsp.features.signature_help import _parse_parameters

        params = _parse_parameters("(src:cid, dst:cid)")
        assert len(params) == 2
        assert params[0].label == "src:cid"
        assert params[1].label == "dst:cid"

    def test_parse_action_detail(self):
        """Parse 'action send(src:cid, dst:cid)' detail."""
        from ivy_lsp.features.signature_help import _parse_parameters

        params = _parse_parameters("action send(src:cid, dst:cid)")
        assert len(params) == 2
        assert params[0].label == "src:cid"

    def test_parse_no_params(self):
        """Detail without parentheses yields empty list."""
        from ivy_lsp.features.signature_help import _parse_parameters

        assert _parse_parameters("relation") == []
        assert _parse_parameters("enum: a, b") == []
        assert _parse_parameters(None) == []

    def test_parse_empty_parens(self):
        """Empty parentheses yield empty list."""
        from ivy_lsp.features.signature_help import _parse_parameters

        assert _parse_parameters("()") == []

    def test_parse_three_params(self):
        """Three parameter signature."""
        from ivy_lsp.features.signature_help import _parse_parameters

        params = _parse_parameters("(a:nat, b:nat, c:nat)")
        assert len(params) == 3
        assert params[2].label == "c:nat"


class TestComputeSignatureHelp:
    def test_trigger_on_open_paren(self, tmp_path):
        """Cursor right after '(' triggers signature help."""
        from ivy_lsp.features.signature_help import compute_signature_help
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = "#lang ivy1.7\naction send(dst:cid)\nafter init { send( }\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        lines = source.split("\n")
        filepath = str(tmp_path / "a.ivy")
        # Cursor right after "send(" on line 2
        result = compute_signature_help(
            indexer, filepath, lines, Position(line=2, character=18)
        )
        assert result is not None
        assert len(result.signatures) >= 1
        assert "send" in result.signatures[0].label

    def test_no_symbol_returns_none(self):
        """Cursor not in a call context returns None."""
        from ivy_lsp.features.signature_help import compute_signature_help

        lines = ["#lang ivy1.7", "type cid"]
        result = compute_signature_help(None, "", lines, Position(line=1, character=5))
        assert result is None

    def test_active_parameter_index(self, tmp_path):
        """After comma, active parameter should advance."""
        from ivy_lsp.features.signature_help import compute_signature_help
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        # Write a valid file so the parser indexes foo with full detail
        indexed_source = (
            "#lang ivy1.7\n"
            "type cid\n"
            "type pkt_num\n"
            "action foo(a:cid, b:cid, c:pkt_num)\n"
        )
        (tmp_path / "a.ivy").write_text(indexed_source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        # Simulate the user typing a call with 2 commas (3rd param active)
        editing_lines = [
            "#lang ivy1.7",
            "type cid",
            "type pkt_num",
            "action foo(a:cid, b:cid, c:pkt_num)",
            "after init { foo(x, y, }",
        ]
        filepath = str(tmp_path / "a.ivy")
        # Line 4: "after init { foo(x, y, }" -- cursor after last comma+space
        result = compute_signature_help(
            indexer, filepath, editing_lines, Position(line=4, character=23)
        )
        assert result is not None
        assert result.signatures[0].active_parameter == 2

    def test_active_parameter_clamped_when_excess_commas(self, tmp_path):
        """Extra commas beyond parameter count clamp active_parameter."""
        from ivy_lsp.features.signature_help import compute_signature_help
        from ivy_lsp.indexer.include_resolver import IncludeResolver
        from ivy_lsp.indexer.workspace_indexer import WorkspaceIndexer
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        source = "#lang ivy1.7\ntype cid\naction send(dst:cid)\n"
        (tmp_path / "a.ivy").write_text(source)
        parser = IvyParserWrapper()
        resolver = IncludeResolver(str(tmp_path))
        indexer = WorkspaceIndexer(str(tmp_path), parser, resolver)
        indexer.index_workspace()

        # send has 1 param, but cursor is after 3 commas
        editing_lines = [
            "#lang ivy1.7",
            "type cid",
            "action send(dst:cid)",
            "after init { send(a, b, c, }",
        ]
        filepath = str(tmp_path / "a.ivy")
        result = compute_signature_help(
            indexer, filepath, editing_lines, Position(line=3, character=27)
        )
        assert result is not None
        # active_parameter on SignatureHelp must be clamped to max valid index (0)
        assert result.active_parameter <= 0


class TestFindCallContext:
    def test_simple_call(self):
        from ivy_lsp.features.signature_help import _find_call_context

        assert _find_call_context("send(x", 6) == ("send", 0)

    def test_second_param(self):
        from ivy_lsp.features.signature_help import _find_call_context

        assert _find_call_context("send(x, y", 9) == ("send", 1)

    def test_nested_parens(self):
        from ivy_lsp.features.signature_help import _find_call_context

        result = _find_call_context("foo(bar(x), ", 12)
        assert result == ("foo", 1)

    def test_no_open_paren(self):
        from ivy_lsp.features.signature_help import _find_call_context

        assert _find_call_context("type cid", 5) is None

    def test_no_function_name(self):
        from ivy_lsp.features.signature_help import _find_call_context

        assert _find_call_context("(x + y)", 4) is None
