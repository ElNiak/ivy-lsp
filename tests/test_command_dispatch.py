"""Tests for workspace/executeCommand dispatch in commands.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


def _register_commands(server_mock=None):
    """Register command handlers and return (server, features, commands)."""
    from ivy_lsp.features.commands import register

    server = server_mock or MagicMock()
    features = {}
    commands = {}

    def fake_feature(method, options=None):
        def decorator(fn):
            features[method] = fn
            return fn
        return decorator

    def fake_command(name):
        def decorator(fn):
            commands[name] = fn
            return fn
        return decorator

    server.feature = fake_feature
    server.command = fake_command
    register(server)
    return server, features, commands


class TestExecuteCommandDispatch:
    """Verify CodeLens commands are registered via server.command()."""

    def _get_command_handler(self, cmd_name):
        server, _, commands = _register_commands()
        server._indexer = None  # triggers early "No indexer" return
        return server, commands.get(cmd_name)

    @pytest.mark.asyncio
    async def test_known_command_dispatches(self):
        _, handler = self._get_command_handler("ivy.showActionRequirements")
        assert handler is not None
        result = await handler("quic.send")
        assert isinstance(result, dict)
        assert "error" in result  # "No indexer available"

    @pytest.mark.asyncio
    async def test_noop_command_returns_none(self):
        _, handler = self._get_command_handler("ivy.noop")
        assert handler is not None
        result = await handler()
        assert result is None

    def test_all_lens_commands_registered(self):
        """Every CodeLens command must be registered via server.command()."""
        _, _, commands = _register_commands()

        expected_commands = [
            "ivy.showActionRequirements",
            "ivy.showPropertyDetails",
            "ivy.navigateToInclude",
            "ivy.showRfcDetails",
            "ivy.noop",
        ]
        for cmd in expected_commands:
            assert cmd in commands, f"{cmd} not registered via server.command()"

    def test_lens_commands_not_double_registered_as_features(self):
        """CodeLens commands must NOT also be registered as features."""
        _, features, _ = _register_commands()

        lens_commands = [
            "ivy.showActionRequirements",
            "ivy.showPropertyDetails",
            "ivy.navigateToInclude",
            "ivy.showRfcDetails",
            "ivy.noop",
        ]
        for cmd in lens_commands:
            assert cmd not in features, (
                f"{cmd} is registered as both feature and command (duplicate)"
            )
