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
