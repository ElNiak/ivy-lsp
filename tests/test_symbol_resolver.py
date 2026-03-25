"""Tests for ivy_lsp.utils.symbol_resolver shared helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from ivy_lsp.infra.utils.symbol_resolver import (
    ensure_deep_parsed,
    lookup_with_dotted_fallback,
)


class TestLookupWithDottedFallback:
    """Test progressive dotted suffix lookup."""

    def test_direct_match(self):
        indexer = MagicMock()
        indexer.lookup_symbol.return_value = ["result"]
        assert lookup_with_dotted_fallback(indexer, "foo") == ["result"]
        indexer.lookup_symbol.assert_called_once_with("foo")

    def test_no_dot_no_match(self):
        indexer = MagicMock()
        indexer.lookup_symbol.return_value = []
        assert lookup_with_dotted_fallback(indexer, "foo") == []

    def test_dotted_fallback_progressive(self):
        indexer = MagicMock()
        indexer.lookup_symbol.side_effect = [
            [],  # "a.b.c" -> no match
            [],  # "b.c" -> no match
            ["found"],  # "c" -> match
        ]
        assert lookup_with_dotted_fallback(indexer, "a.b.c") == ["found"]

    def test_dotted_fallback_stops_at_first_match(self):
        indexer = MagicMock()
        indexer.lookup_symbol.side_effect = [
            [],  # "a.b.c" -> no match
            ["found"],  # "b.c" -> match
        ]
        result = lookup_with_dotted_fallback(indexer, "a.b.c")
        assert result == ["found"]
        assert indexer.lookup_symbol.call_count == 2


class TestEnsureDeepParsed:
    """Test demand-driven deep parse guard."""

    def test_calls_deep_parse_when_available(self):
        indexer = MagicMock(spec=["deep_parse_on_demand"])
        ensure_deep_parsed(indexer, "/path/to/file.ivy")
        indexer.deep_parse_on_demand.assert_called_once_with("/path/to/file.ivy")

    def test_no_op_when_method_missing(self):
        indexer = MagicMock(spec=[])
        ensure_deep_parsed(indexer, "/path/to/file.ivy")  # should not raise

    def test_no_op_when_indexer_is_none(self):
        ensure_deep_parsed(None, "/path/to/file.ivy")  # should not raise
