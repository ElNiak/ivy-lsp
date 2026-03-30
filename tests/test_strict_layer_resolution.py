"""Tests for strict layer resolution — no cross-layer proximity fallback."""

import os

import pytest

from ivy_lsp.core.indexer.include_resolver import IncludeResolver
from ivy_lsp.core.workspace.detection import WorkspaceLayer


class TestNoProximityFallback:
    """Cross-layer proximity fallback is disabled."""

    @pytest.fixture
    def two_layer_workspace(self, tmp_path):
        """Two independent layers with a colliding basename."""
        ws = tmp_path / "ws"
        (ws / "proto_a").mkdir(parents=True)
        (ws / "proto_a" / "types.ivy").write_text("#lang ivy1.7\n# proto_a types")
        (ws / "proto_a" / "main.ivy").write_text("#lang ivy1.7\ninclude helper")

        (ws / "proto_b").mkdir(parents=True)
        (ws / "proto_b" / "helper.ivy").write_text("#lang ivy1.7\n# proto_b helper")

        layers = [
            WorkspaceLayer(id="a", include_paths=["proto_a"], priority=1),
            WorkspaceLayer(id="b", include_paths=["proto_b"], priority=2),
        ]
        resolver = IncludeResolver(
            workspace_root=str(ws),
            include_paths=["proto_a", "proto_b"],
            workspace_layers=layers,
        )
        resolver.create_staging_directory()
        resolver.build_layered_staging()
        return resolver, ws

    def test_cross_layer_blocked(self, two_layer_workspace):
        """Layer a cannot resolve 'helper' from layer b without depends_on."""
        resolver, ws = two_layer_workspace
        from_file = str(ws / "proto_a" / "main.ivy")
        result = resolver.resolve("helper", from_file)
        # helper.ivy only exists in layer b — layer a has no depends_on
        assert result is None

    def test_depends_on_still_works(self, tmp_path):
        """Explicit depends_on still resolves cross-layer."""
        ws = tmp_path / "ws"
        (ws / "base").mkdir(parents=True)
        (ws / "base" / "shared.ivy").write_text("#lang ivy1.7\n# shared")
        (ws / "ext").mkdir(parents=True)
        (ws / "ext" / "main.ivy").write_text("#lang ivy1.7\ninclude shared")

        layers = [
            WorkspaceLayer(id="base_layer", include_paths=["base"], priority=1),
            WorkspaceLayer(
                id="ext_layer",
                include_paths=["ext"],
                priority=2,
                depends_on=["base_layer"],
            ),
        ]
        resolver = IncludeResolver(
            workspace_root=str(ws),
            include_paths=["base", "ext"],
            workspace_layers=layers,
        )
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        from_file = str(ws / "ext" / "main.ivy")
        result = resolver.resolve("shared", from_file)
        assert result is not None
        assert "base" in result

    def test_same_layer_resolves(self, tmp_path):
        """Files within the same layer resolve normally."""
        ws = tmp_path / "ws"
        (ws / "proto").mkdir(parents=True)
        (ws / "proto" / "types.ivy").write_text("#lang ivy1.7\n# types")
        (ws / "proto" / "main.ivy").write_text("#lang ivy1.7\ninclude types")

        layers = [
            WorkspaceLayer(id="proto", include_paths=["proto"], priority=1),
        ]
        resolver = IncludeResolver(
            workspace_root=str(ws),
            include_paths=["proto"],
            workspace_layers=layers,
        )
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        from_file = str(ws / "proto" / "main.ivy")
        result = resolver.resolve("types", from_file)
        assert result is not None
        assert "proto" in result


class TestNoWorkspaceRootFallback:
    """Workspace root fallback is disabled when layers are active."""

    def test_workspace_root_blocked_with_layers(self, tmp_path):
        """Files at workspace root cannot be reached when layers are active."""
        ws = tmp_path / "ws"
        ws.mkdir(parents=True)
        # File at workspace root that would match
        (ws / "stray.ivy").write_text("#lang ivy1.7\n# stray file at root")
        (ws / "proto").mkdir(parents=True)
        (ws / "proto" / "main.ivy").write_text("#lang ivy1.7\ninclude stray")

        layers = [
            WorkspaceLayer(id="proto", include_paths=["proto"], priority=1),
        ]
        resolver = IncludeResolver(
            workspace_root=str(ws),
            include_paths=["proto"],
            workspace_layers=layers,
        )
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        from_file = str(ws / "proto" / "main.ivy")
        result = resolver.resolve("stray", from_file)
        # stray.ivy is at workspace root, not in any layer — should not resolve
        assert result is None
