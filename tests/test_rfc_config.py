# tests/test_rfc_config.py
"""Tests for RFC-related configuration."""

import os
from unittest.mock import patch

from ivy_lsp.infra.config import ServerConfig, reset_config


class TestRfcConfig:
    def teardown_method(self):
        reset_config()

    def test_default_values(self):
        with patch.dict(os.environ, {}, clear=False):
            cfg = ServerConfig.from_env()
        assert cfg.rfc_cache_ttl == 3600
        assert cfg.rfc_offline is False
        assert cfg.rfc_cache_dir is None
        assert cfg.rfc_local_dir is None

    def test_custom_values(self):
        env = {
            "IVY_LSP_RFC_CACHE_TTL": "7200",
            "IVY_LSP_RFC_OFFLINE": "1",
            "IVY_LSP_RFC_CACHE_DIR": "/tmp/rfc-cache",
            "IVY_LSP_RFC_LOCAL_DIR": "/tmp/rfc-local",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = ServerConfig.from_env()
        assert cfg.rfc_cache_ttl == 7200
        assert cfg.rfc_offline is True
        assert cfg.rfc_cache_dir == "/tmp/rfc-cache"
        assert cfg.rfc_local_dir == "/tmp/rfc-local"
