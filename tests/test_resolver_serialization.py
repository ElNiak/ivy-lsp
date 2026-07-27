"""Tests for IncludeResolver config serialization."""

from ivy_lsp.core.indexer.include_resolver import IncludeResolver


class TestResolverSerialization:
    def test_to_config_dict(self, tmp_path):
        resolver = IncludeResolver(
            str(tmp_path),
            ivy_include_path="/opt/ivy/include/1.7",
            exclude_paths=["build"],
            include_paths=["src"],
        )
        d = resolver.to_config_dict()
        assert d["workspace_root"] == str(tmp_path)
        assert d["ivy_include_path"] == "/opt/ivy/include/1.7"
        assert d["exclude_paths"] == ["build"]

    def test_from_config_roundtrip(self, tmp_path):
        original = IncludeResolver(str(tmp_path), exclude_paths=["build"])
        d = original.to_config_dict()
        restored = IncludeResolver.from_config(d)
        assert restored._workspace_root == original._workspace_root
        assert restored._exclude_paths == original._exclude_paths
