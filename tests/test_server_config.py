"""Tests for ivy_lsp.config.ServerConfig."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from ivy_lsp.config import ServerConfig, get_config, reset_config


class TestServerConfigFromEnv:
    """Test ServerConfig.from_env() reads environment variables correctly."""

    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_defaults(self):
        """All defaults match when no IVY_LSP_* env vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = ServerConfig.from_env()
        assert cfg.log_level == "INFO"
        assert cfg.activity_level == "phase"
        assert cfg.workspace is None
        assert cfg.workspace_hint is None
        assert cfg.include_paths == []
        assert cfg.exclude_paths == []
        assert cfg.bulk_analysis is True
        assert cfg.bulk_analysis_t2 is True
        assert cfg.bulk_compile is True
        assert cfg.compile_workers == 1
        assert cfg.compile_timeout == 300.0
        assert cfg.compile_cache_ttl == 600.0
        assert cfg.max_concurrent_tools == 4
        assert cfg.fast_index_workers == 4
        assert cfg.parse_workers == 0
        assert cfg.bulk_workers == 4
        assert cfg.lock_timeout == 30.0
        assert cfg.verify_timeout == 120.0
        assert cfg.tool_compile_timeout == 300.0
        assert cfg.show_model_timeout == 30.0

    def test_bulk_analysis_disabled(self):
        with patch.dict(os.environ, {"IVY_LSP_BULK_ANALYSIS": "0"}):
            cfg = ServerConfig.from_env()
        assert cfg.bulk_analysis is False

    def test_bulk_analysis_t2_disabled(self):
        with patch.dict(os.environ, {"IVY_LSP_BULK_ANALYSIS_T2": "0"}):
            cfg = ServerConfig.from_env()
        assert cfg.bulk_analysis_t2 is False

    def test_bulk_compile_disabled(self):
        with patch.dict(os.environ, {"IVY_LSP_BULK_COMPILE": "0"}):
            cfg = ServerConfig.from_env()
        assert cfg.bulk_compile is False

    def test_csv_paths(self):
        with patch.dict(
            os.environ,
            {
                "IVY_LSP_INCLUDE_PATHS": "a,b,c",
                "IVY_LSP_EXCLUDE_PATHS": "x , y",
            },
        ):
            cfg = ServerConfig.from_env()
        assert cfg.include_paths == ["a", "b", "c"]
        assert cfg.exclude_paths == ["x", "y"]

    def test_csv_empty_segments_stripped(self):
        with patch.dict(os.environ, {"IVY_LSP_INCLUDE_PATHS": ",a,,b,"}):
            cfg = ServerConfig.from_env()
        assert cfg.include_paths == ["a", "b"]

    def test_integer_env_vars(self):
        with patch.dict(
            os.environ,
            {
                "IVY_LSP_COMPILE_WORKERS": "8",
                "IVY_LSP_FAST_INDEX_WORKERS": "2",
                "IVY_LSP_PARSE_WORKERS": "3",
                "IVY_LSP_BULK_WORKERS": "6",
                "IVY_LSP_MAX_CONCURRENT_TOOLS": "10",
            },
        ):
            cfg = ServerConfig.from_env()
        assert cfg.compile_workers == 8
        assert cfg.fast_index_workers == 2
        assert cfg.parse_workers == 3
        assert cfg.bulk_workers == 6
        assert cfg.max_concurrent_tools == 10

    def test_float_env_vars(self):
        with patch.dict(
            os.environ,
            {
                "IVY_LSP_COMPILE_TIMEOUT": "60",
                "IVY_LSP_COMPILE_CACHE_TTL": "120",
                "IVY_LSP_LOCK_TIMEOUT": "10",
                "IVY_LSP_VERIFY_TIMEOUT": "200",
                "IVY_LSP_TOOL_COMPILE_TIMEOUT": "500",
                "IVY_LSP_SHOW_MODEL_TIMEOUT": "15",
            },
        ):
            cfg = ServerConfig.from_env()
        assert cfg.compile_timeout == 60.0
        assert cfg.compile_cache_ttl == 120.0
        assert cfg.lock_timeout == 10.0
        assert cfg.verify_timeout == 200.0
        assert cfg.tool_compile_timeout == 500.0
        assert cfg.show_model_timeout == 15.0

    def test_log_level_uppercased(self):
        with patch.dict(os.environ, {"IVY_LSP_LOG_LEVEL": "debug"}):
            cfg = ServerConfig.from_env()
        assert cfg.log_level == "DEBUG"

    def test_workspace_and_hint(self):
        with patch.dict(
            os.environ,
            {
                "IVY_LSP_WORKSPACE": "/tmp/ws",
                "IVY_LSP_WORKSPACE_HINT": "proto",
            },
        ):
            cfg = ServerConfig.from_env()
        assert cfg.workspace == "/tmp/ws"
        assert cfg.workspace_hint == "proto"

    def test_compile_workers_floor_at_1(self):
        with patch.dict(os.environ, {"IVY_LSP_COMPILE_WORKERS": "0"}):
            cfg = ServerConfig.from_env()
        assert cfg.compile_workers == 1

    def test_invalid_int_falls_back_to_default(self):
        with patch.dict(os.environ, {"IVY_LSP_COMPILE_WORKERS": "abc"}):
            cfg = ServerConfig.from_env()
        assert cfg.compile_workers == 1

    def test_invalid_float_falls_back_to_default(self):
        with patch.dict(os.environ, {"IVY_LSP_COMPILE_TIMEOUT": "abc"}):
            cfg = ServerConfig.from_env()
        assert cfg.compile_timeout == 300.0

    def test_frozen(self):
        cfg = ServerConfig.from_env()
        with pytest.raises(AttributeError):
            cfg.log_level = "DEBUG"  # type: ignore[misc]


class TestGetConfig:
    """Test the singleton accessor."""

    def setup_method(self):
        reset_config()

    def teardown_method(self):
        reset_config()

    def test_singleton_returns_same_instance(self):
        a = get_config()
        b = get_config()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = get_config()
        reset_config()
        b = get_config()
        assert a is not b
