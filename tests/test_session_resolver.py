"""Tests for the canonical resolve_session_id() function."""

import os

from ivy_lsp.infra.observability.session import (
    reset_session_cache,
    resolve_session_id,
    workspace_hash,
)


class TestResolveSessionId:
    """Priority: hook_payload > CLAUDE_SESSION_ID > CLAUDE_CODE_SESSION_ID > IVY_SESSION_ID > file > unknown."""

    def setup_method(self):
        reset_session_cache()

    def test_fallback_returns_unknown(self, monkeypatch):
        for var in (
            "IVY_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "IVY_WORKSPACE_ROOT",
        ):
            monkeypatch.delenv(var, raising=False)
        assert resolve_session_id(session_dir="/nonexistent") == "unknown"

    def test_ivy_session_id_wins_over_file(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("IVY_SESSION_ID", "from-ivy-env")
        assert resolve_session_id() == "from-ivy-env"

    def test_claude_session_id_wins_over_ivy(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "from-claude")
        monkeypatch.setenv("IVY_SESSION_ID", "from-ivy-env")
        assert resolve_session_id() == "from-claude"

    def test_claude_code_session_id_wins_over_ivy(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-code")
        monkeypatch.setenv("IVY_SESSION_ID", "from-ivy-env")
        assert resolve_session_id() == "from-code"

    def test_hook_payload_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "from-claude")
        assert (
            resolve_session_id(hook_payload={"session_id": "from-hook"}) == "from-hook"
        )

    def test_file_fallback(self, monkeypatch, tmp_path):
        for var in (
            "IVY_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        ws_root = str(tmp_path / "project")
        monkeypatch.setenv("IVY_WORKSPACE_ROOT", ws_root)
        wh = workspace_hash(ws_root)
        session_file = tmp_path / f"ivy-session-{wh}.id"
        session_file.write_text("file-session\n")
        reset_session_cache()
        assert resolve_session_id(session_dir=str(tmp_path)) == "file-session"


class TestWorkspaceHash:
    def test_returns_12_char_hex(self):
        result = workspace_hash("/some/path")
        assert len(result) == 12
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        assert workspace_hash("/a") == workspace_hash("/a")
        assert workspace_hash("/a") != workspace_hash("/b")
