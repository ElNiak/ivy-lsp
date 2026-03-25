"""Tests for ivy-lsp index and detect CLI subcommands in __main__.py."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _suppress_log_rotation():
    """Prevent main() from opening /tmp/ivy-lsp.log during tests."""
    with patch("ivy_lsp.__main__._setup_log_rotation"):
        yield


class TestIndexSubcommand:
    """Verify that `ivy-lsp index` dispatches to cli_index."""

    @patch("ivy_lsp.__main__.sys")
    def test_index_dispatches(self, mock_sys):
        """argv=['ivy-lsp', 'index', '--all'] should call cli_index(['--all'])."""
        mock_sys.argv = ["ivy-lsp", "index", "--all"]
        mock_sys.stderr = sys.stderr
        mock_sys.exit = sys.exit

        with patch("ivy_lsp.index_builder.cli_index", return_value=0) as mock_cli:
            with pytest.raises(SystemExit) as exc_info:
                from ivy_lsp.__main__ import main

                main()
            assert exc_info.value.code == 0
            mock_cli.assert_called_once_with(["--all"])

    @patch("ivy_lsp.__main__.sys")
    def test_index_propagates_exit_code(self, mock_sys):
        mock_sys.argv = ["ivy-lsp", "index", "protocol-testing/quic/"]
        mock_sys.stderr = sys.stderr
        mock_sys.exit = sys.exit

        with patch("ivy_lsp.index_builder.cli_index", return_value=1) as mock_cli:
            with pytest.raises(SystemExit) as exc_info:
                from ivy_lsp.__main__ import main

                main()
            assert exc_info.value.code == 1


class TestDetectSubcommand:
    """Verify that `ivy-lsp detect` dispatches to WorkspaceContext.detect."""

    @patch("ivy_lsp.__main__.sys")
    def test_detect_outputs_json(self, mock_sys, capsys):
        mock_sys.argv = ["ivy-lsp", "detect", "/some/dir"]
        mock_sys.stderr = sys.stderr
        mock_sys.exit = sys.exit

        detect_result = {
            "workspace_root": "/test",
            "project_type": "panther",
            "detected_by": "marker",
            "protocols": [],
            "has_index": False,
            "staleness": {},
        }
        with patch(
            "ivy_lsp.core.workspace.context.WorkspaceContext.detect",
            return_value=detect_result,
        ) as mock_detect:
            with pytest.raises(SystemExit) as exc_info:
                from ivy_lsp.__main__ import main

                main()
            assert exc_info.value.code == 0
            mock_detect.assert_called_once_with("/some/dir")
            output = json.loads(capsys.readouterr().out)
            assert output["project_type"] == "panther"
