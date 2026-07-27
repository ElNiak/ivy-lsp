"""Tests for Z3 detection utility."""

import os
from unittest.mock import patch

import pytest

from ivy_lsp.core.environment import detect_z3_dir


class TestDetectZ3Dir:
    """Tests for detect_z3_dir()."""

    def setup_method(self):
        detect_z3_dir.cache_clear()

    def test_returns_z3dir_env_var_when_set(self, tmp_path):
        z3_dir = str(tmp_path / "z3")
        os.makedirs(z3_dir, exist_ok=True)
        with patch.dict(os.environ, {"Z3DIR": z3_dir}):
            assert detect_z3_dir() == z3_dir

    def test_returns_none_when_nothing_found(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.isfile", return_value=False):
                with patch("subprocess.run", side_effect=FileNotFoundError):
                    assert detect_z3_dir() is None

    def test_returns_brew_prefix_on_macos(self, tmp_path):
        brew_z3 = str(tmp_path / "homebrew-z3")
        os.makedirs(brew_z3, exist_ok=True)
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = brew_z3
                with patch("ivy_lsp.core.environment.sys") as mock_sys:
                    mock_sys.platform = "darwin"
                    assert detect_z3_dir() == brew_z3

    def test_returns_usr_local_when_header_exists(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with patch("os.path.isfile") as mock_isfile:
                    mock_isfile.side_effect = lambda p: p == "/usr/local/include/z3++.h"
                    assert detect_z3_dir() == "/usr/local"

    def test_returns_usr_when_header_exists(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                with patch("os.path.isfile") as mock_isfile:
                    mock_isfile.side_effect = lambda p: p == "/usr/include/z3++.h"
                    assert detect_z3_dir() == "/usr"
