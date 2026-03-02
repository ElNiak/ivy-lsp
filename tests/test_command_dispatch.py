"""Tests for workspace/executeCommand dispatch in commands.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


def _register_commands(server_mock=None):
    """Register command handlers and return (server, registered_handlers)."""
    from ivy_lsp.features.commands import register

    server = server_mock or MagicMock()
    registered = {}

    def fake_feature(method, options=None):
        def decorator(fn):
            registered[method] = fn
            return fn
        return decorator

    server.feature = fake_feature
    register(server)
    return server, registered


class TestExecuteCommandDispatch:
    """Verify workspace/executeCommand routes to correct handlers."""

    def _get_dispatcher(self):
        server, registered = _register_commands()
        server._indexer = None  # triggers early "No indexer" return
        return server, registered.get(lsp.WORKSPACE_EXECUTE_COMMAND)

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self):
        server, dispatcher = self._get_dispatcher()
        assert dispatcher is not None
        params = lsp.ExecuteCommandParams(
            command="ivy.nonExistentCommand",
            arguments=[],
        )
        result = await dispatcher(params)
        assert "error" in result
        assert "Unknown command" in result["error"]

    @pytest.mark.asyncio
    async def test_known_command_dispatches(self):
        server, dispatcher = self._get_dispatcher()
        params = lsp.ExecuteCommandParams(
            command="ivy.showActionRequirements",
            arguments=["quic.send"],
        )
        result = await dispatcher(params)
        # Should return error dict (no indexer) -- but the dispatch worked
        assert isinstance(result, dict)
        assert "error" in result  # "No indexer available"

    @pytest.mark.asyncio
    async def test_noop_command_returns_none(self):
        server, dispatcher = self._get_dispatcher()
        params = lsp.ExecuteCommandParams(
            command="ivy.noop",
            arguments=[],
        )
        result = await dispatcher(params)
        assert result is None

    def test_all_lens_commands_in_dispatch_table(self):
        """Every CodeLens command must be registered in the dispatch table."""
        _, registered = _register_commands()
        assert lsp.WORKSPACE_EXECUTE_COMMAND in registered

        expected_commands = [
            "ivy.showActionRequirements",
            "ivy.showPropertyDetails",
            "ivy.navigateToInclude",
            "ivy.showRfcDetails",
            "ivy.noop",
        ]
        # Verify all are registered as custom features too
        for cmd in expected_commands:
            assert cmd in registered, f"{cmd} not registered as a feature"
