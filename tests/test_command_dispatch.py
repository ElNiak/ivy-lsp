"""Tests for CodeLens command dispatch in commands.py."""

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


LENS_COMMANDS = [
    "ivy.showActionRequirements",
    "ivy.showPropertyDetails",
    "ivy.navigateToInclude",
    "ivy.showRfcDetails",
    "ivy.noop",
]


class TestExecuteCommandDispatch:
    """Verify CodeLens commands are registered via server.feature()."""

    def _get_feature_handler(self, cmd_name):
        server, features, _ = _register_commands()
        server.indexer = None  # triggers early "No indexer" return
        return server, features.get(cmd_name)

    @pytest.mark.asyncio
    async def test_known_command_dispatches(self):
        _, handler = self._get_feature_handler("ivy.showActionRequirements")
        assert handler is not None
        result = await handler(["quic.send"])
        assert isinstance(result, dict)
        assert "error" in result  # "No indexer available"

    @pytest.mark.asyncio
    async def test_noop_command_returns_none(self):
        _, handler = self._get_feature_handler("ivy.noop")
        assert handler is not None
        result = await handler()
        assert result is None

    def test_all_lens_commands_registered_as_features(self):
        """Every CodeLens command must be registered via server.feature()."""
        _, features, _ = _register_commands()

        for cmd in LENS_COMMANDS:
            assert cmd in features, f"{cmd} not registered via server.feature()"

    def test_lens_commands_not_in_execute_command_provider(self):
        """CodeLens commands must NOT be in server.command() (avoids duplicate registration)."""
        _, _, commands = _register_commands()

        for cmd in LENS_COMMANDS:
            assert cmd not in commands, (
                f"{cmd} registered via server.command() — will cause "
                f"duplicate VS Code command registration"
            )
