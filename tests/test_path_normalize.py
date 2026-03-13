"""Tests for ivy_lsp.utils.path_normalize."""
import os
import sys
from pathlib import Path

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.utils.path_normalize import (
    normalize_ivy_path,
    normalize_file_filter,
    strip_prefix,
    ensure_prefix,
    relativize_path,
)


class TestNormalizeIvyPath:
    def test_path_with_prefix_resolves(self, tmp_path):
        (tmp_path / "protocol-testing" / "quic").mkdir(parents=True)
        f = tmp_path / "protocol-testing" / "quic" / "test.ivy"
        f.touch()
        result = normalize_ivy_path("protocol-testing/quic/test.ivy", str(tmp_path))
        assert result == str(f)

    def test_path_without_prefix_falls_back(self, tmp_path):
        (tmp_path / "protocol-testing" / "quic").mkdir(parents=True)
        f = tmp_path / "protocol-testing" / "quic" / "test.ivy"
        f.touch()
        result = normalize_ivy_path("quic/test.ivy", str(tmp_path))
        assert result == str(f)

    def test_absolute_path_returned_as_is(self, tmp_path):
        abs_path = str(tmp_path / "any" / "file.ivy")
        result = normalize_ivy_path(abs_path, str(tmp_path))
        assert result == abs_path

    def test_nonexistent_returns_direct(self, tmp_path):
        result = normalize_ivy_path("nofile.ivy", str(tmp_path))
        assert result == str(tmp_path / "nofile.ivy")


class TestNormalizeFileFilter:
    def test_exact_match(self):
        refs = ["/a/b/quic.ivy", "/a/b/tls.ivy"]
        assert normalize_file_filter("/a/b/quic.ivy", refs) == "/a/b/quic.ivy"

    def test_basename_match(self):
        refs = ["/long/path/quic.ivy", "/other/tls.ivy"]
        assert normalize_file_filter("quic.ivy", refs) == "/long/path/quic.ivy"

    def test_endswith_match(self):
        refs = ["/ws/protocol-testing/quic/quic_stack/types.ivy"]
        assert normalize_file_filter("quic/quic_stack/types.ivy", refs) == refs[0]

    def test_no_match_returns_none(self):
        assert normalize_file_filter("nope.ivy", ["/a/b.ivy"]) is None


class TestStripAndEnsurePrefix:
    def test_strip_removes_prefix(self):
        assert strip_prefix("protocol-testing/quic/x.ivy") == "quic/x.ivy"

    def test_strip_noop_without_prefix(self):
        assert strip_prefix("quic/x.ivy") == "quic/x.ivy"

    def test_ensure_adds_prefix(self):
        assert ensure_prefix("quic/x.ivy") == "protocol-testing/quic/x.ivy"

    def test_ensure_noop_with_prefix(self):
        assert ensure_prefix("protocol-testing/quic/x.ivy") == "protocol-testing/quic/x.ivy"


class TestRelativizePath:
    def test_strips_workspace_root(self):
        assert relativize_path("/ws/root/foo/bar.ivy", "/ws/root") == "foo/bar.ivy"

    def test_noop_when_no_match(self):
        assert relativize_path("/other/bar.ivy", "/ws/root") == "/other/bar.ivy"

    def test_empty_inputs(self):
        assert relativize_path("", "/ws") == ""
        assert relativize_path("/ws/foo", "") == "/ws/foo"
