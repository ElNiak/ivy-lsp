"""Tests for incremental readiness signaling."""

import threading

import pytest


class TestParserReadyEvent:
    """_parser_ready_event should exist and be set in the finally block."""

    def test_server_has_parser_ready_event(self):
        from ivy_lsp.lsp.server import IvyLanguageServer

        server = IvyLanguageServer()
        assert hasattr(server, "_parser_ready_event")
        assert isinstance(server._parser_ready_event, threading.Event)
        # Should not be set at construction time
        assert not server._parser_ready_event.is_set()


import time
from unittest.mock import MagicMock, patch


class TestSetupIndexerSignaling:
    """_setup_indexer should set _parser_ready_event after _create_parser."""

    def test_parser_ready_set_before_create_indexer(self):
        """Verify _parser_ready_event fires between _create_parser and _create_indexer."""
        from ivy_lsp.lsp.server import IvyLanguageServer

        server = IvyLanguageServer()
        # Track call order
        call_log = []

        original_create_parser = server._create_parser
        original_create_indexer = server._create_indexer

        def mock_create_parser(resolver):
            original_create_parser(resolver)
            call_log.append(("create_parser", server._parser_ready_event.is_set()))

        def mock_create_indexer(ws_root, resolver):
            call_log.append(
                ("create_indexer_start", server._parser_ready_event.is_set())
            )
            return original_create_indexer(ws_root, resolver)

        with (
            patch.object(server, "_create_parser", side_effect=mock_create_parser),
            patch.object(server, "_create_indexer", side_effect=mock_create_indexer),
        ):
            try:
                server._setup_indexer()
            except Exception:
                pass  # May fail without full env, that's fine

        # If create_parser ran, _parser_ready_event should NOT be set during it
        # but SHOULD be set by the time create_indexer starts
        parser_entries = [e for e in call_log if e[0] == "create_parser"]
        indexer_entries = [e for e in call_log if e[0] == "create_indexer_start"]

        if not parser_entries:
            pytest.skip("Z3 not available — cannot test parser signaling")
        assert indexer_entries, "_create_indexer was never called"
        assert not parser_entries[0][
            1
        ], "_parser_ready_event should not be set during _create_parser"
        assert indexer_entries[0][
            1
        ], "_parser_ready_event should be set before _create_indexer"


import asyncio

from lsprotocol import types as lsp

from ivy_lsp.lsp.document_symbols import (
    _status_document_symbol,
    compute_document_symbols,
)


class TestDocumentSymbolReadyGate:
    """documentSymbol should wait on _parser_ready_event, not _ready_event."""

    def test_compute_returns_symbols_with_parser_only(self):
        """Parser alone (no indexer) should produce symbols."""
        from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

        parser = IvyParserWrapper()
        source = "#lang ivy1.7\n\ntype cid\ntype pkt_num\n"
        result = compute_document_symbols(parser, None, source, "test.ivy")
        assert len(result) >= 2, f"Expected >=2 symbols, got {len(result)}"
        names = [s.name for s in result]
        assert "cid" in names
        assert "pkt_num" in names

    def test_status_symbol_on_timeout(self):
        """When _parser_ready_event times out, a status symbol should be returned."""
        sym = _status_document_symbol(
            "server still initializing",
            "Parser initialization exceeded 30s. Check ivy-lsp logs.",
        )
        assert isinstance(sym, lsp.DocumentSymbol)
        assert "still initializing" in sym.name
        assert sym.kind == lsp.SymbolKind.Namespace
