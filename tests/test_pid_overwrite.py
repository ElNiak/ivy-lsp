"""Test that the ivy_lsp process overwrites the PID file with its own PID."""

import os


def test_pid_overwrite_writes_current_pid(tmp_path, monkeypatch):
    """When IVY_PID_FILE is set, __main__ should overwrite it with os.getpid()."""
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("99999")  # Simulate stale wrapper PID

    monkeypatch.setenv("IVY_PID_FILE", str(pid_file))

    from ivy_lsp.__main__ import _overwrite_pid_file

    result = _overwrite_pid_file()

    assert pid_file.read_text().strip() == str(os.getpid())
    assert result == str(pid_file)


def test_pid_overwrite_noop_when_no_env(monkeypatch):
    """When IVY_PID_FILE is not set, _overwrite_pid_file should return None."""
    monkeypatch.delenv("IVY_PID_FILE", raising=False)

    from ivy_lsp.__main__ import _overwrite_pid_file

    assert _overwrite_pid_file() is None


def test_pid_overwrite_handles_missing_file(tmp_path, monkeypatch):
    """When the PID file doesn't exist, overwrite should create it."""
    pid_file = tmp_path / "nonexistent.pid"
    monkeypatch.setenv("IVY_PID_FILE", str(pid_file))

    from ivy_lsp.__main__ import _overwrite_pid_file

    result = _overwrite_pid_file()

    assert pid_file.read_text().strip() == str(os.getpid())
    assert result == str(pid_file)
