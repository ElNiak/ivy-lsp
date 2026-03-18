"""Tests for cross-layer dependency injection in layered staging (P1)."""

import os

from ivy_lsp.indexer.include_resolver import IncludeResolver
from ivy_lsp.workspace_detection import WorkspaceLayer


def _make_layer_workspace(tmp_path):
    """Create a workspace with quic, minip, and apt layers for testing."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # quic layer
    quic = ws / "protocol-testing" / "quic"
    quic.mkdir(parents=True)
    (quic / "quic_types.ivy").write_text("#lang ivy1.7\ntype quic_type\n")
    (quic / "quic_frame.ivy").write_text("#lang ivy1.7\ninclude quic_types\n")

    # minip layer
    minip = ws / "protocol-testing" / "minip"
    minip.mkdir(parents=True)
    (minip / "minip_types.ivy").write_text("#lang ivy1.7\ntype minip_type\n")

    # apt layer (depends on quic and minip)
    apt = ws / "protocol-testing" / "apt"
    apt.mkdir(parents=True)
    (apt / "apt_model.ivy").write_text(
        "#lang ivy1.7\ninclude quic_types\ninclude minip_types\n"
    )
    (apt / "apt_local.ivy").write_text("#lang ivy1.7\ntype apt_local\n")

    return ws


class TestBuildLayeredStagingWithDependsOn:
    """Test that APT staging contains quic/minip files via depends_on."""

    def test_apt_staging_contains_quic_files(self, tmp_path):
        ws = _make_layer_workspace(tmp_path)
        layers = [
            WorkspaceLayer(
                id="quic", include_paths=["protocol-testing/quic"], priority=1
            ),
            WorkspaceLayer(
                id="minip", include_paths=["protocol-testing/minip"], priority=2
            ),
            WorkspaceLayer(
                id="apt",
                include_paths=["protocol-testing/apt"],
                priority=3,
                depends_on=["quic", "minip"],
            ),
        ]
        resolver = IncludeResolver(str(ws), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        apt_dir = resolver._partition_staging["apt"]
        # APT should have its own files plus quic + minip files
        staged_names = set(os.listdir(apt_dir))
        assert "apt_model.ivy" in staged_names
        assert "apt_local.ivy" in staged_names
        assert "quic_types.ivy" in staged_names
        assert "quic_frame.ivy" in staged_names
        assert "minip_types.ivy" in staged_names


class TestResolveCrossLayerViaDependencyInjection:
    """Test that APT files can resolve quic includes via dependency injection."""

    def test_apt_file_resolves_quic_include(self, tmp_path):
        ws = _make_layer_workspace(tmp_path)
        layers = [
            WorkspaceLayer(
                id="quic", include_paths=["protocol-testing/quic"], priority=1
            ),
            WorkspaceLayer(
                id="minip", include_paths=["protocol-testing/minip"], priority=2
            ),
            WorkspaceLayer(
                id="apt",
                include_paths=["protocol-testing/apt"],
                priority=3,
                depends_on=["quic", "minip"],
            ),
        ]
        resolver = IncludeResolver(str(ws), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        apt_model = str(ws / "protocol-testing" / "apt" / "apt_model.ivy")
        result = resolver.resolve("quic_types", apt_model)
        assert result is not None
        assert result.endswith("quic_types.ivy")


class TestOwnFileTakesPrecedence:
    """Test that a layer's own file wins over an injected dependency file."""

    def test_own_file_wins(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()

        # quic layer has shared.ivy
        quic = ws / "protocol-testing" / "quic"
        quic.mkdir(parents=True)
        (quic / "shared.ivy").write_text("#lang ivy1.7\n# quic version\n")

        # apt layer also has shared.ivy
        apt = ws / "protocol-testing" / "apt"
        apt.mkdir(parents=True)
        (apt / "shared.ivy").write_text("#lang ivy1.7\n# apt version\n")
        (apt / "user.ivy").write_text("#lang ivy1.7\ninclude shared\n")

        layers = [
            WorkspaceLayer(
                id="quic", include_paths=["protocol-testing/quic"], priority=1
            ),
            WorkspaceLayer(
                id="apt",
                include_paths=["protocol-testing/apt"],
                priority=3,
                depends_on=["quic"],
            ),
        ]
        resolver = IncludeResolver(str(ws), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        apt_dir = resolver._partition_staging["apt"]
        # shared.ivy symlink in apt staging should point to apt's own version
        link_target = os.path.realpath(os.path.join(apt_dir, "shared.ivy"))
        assert "apt" in link_target
        assert "quic" not in link_target


class TestCircularDependencyDetected:
    """Test that cycles in depends_on are detected and handled gracefully."""

    def test_cycle_detected_no_crash(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()

        a = ws / "protocol-testing" / "a"
        a.mkdir(parents=True)
        (a / "a.ivy").write_text("#lang ivy1.7\ntype a\n")

        b = ws / "protocol-testing" / "b"
        b.mkdir(parents=True)
        (b / "b.ivy").write_text("#lang ivy1.7\ntype b\n")

        layers = [
            WorkspaceLayer(
                id="a",
                include_paths=["protocol-testing/a"],
                priority=1,
                depends_on=["b"],
            ),
            WorkspaceLayer(
                id="b",
                include_paths=["protocol-testing/b"],
                priority=2,
                depends_on=["a"],
            ),
        ]
        resolver = IncludeResolver(str(ws), workspace_layers=layers)
        resolver.create_staging_directory()
        # Should not raise — cycles are logged and injection is skipped
        resolver.build_layered_staging()

        # Both layers should still have their own files staged
        a_dir = resolver._partition_staging["a"]
        b_dir = resolver._partition_staging["b"]
        assert "a.ivy" in os.listdir(a_dir)
        assert "b.ivy" in os.listdir(b_dir)
        # But no cross-injection (due to cycle)
        assert "b.ivy" not in os.listdir(a_dir)
        assert "a.ivy" not in os.listdir(b_dir)


class TestDependencyOrderMatters:
    """Test that first dependency listed wins on basename collision."""

    def test_first_dep_wins_on_collision(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()

        dep1 = ws / "protocol-testing" / "dep1"
        dep1.mkdir(parents=True)
        (dep1 / "common.ivy").write_text("#lang ivy1.7\n# dep1 version\n")

        dep2 = ws / "protocol-testing" / "dep2"
        dep2.mkdir(parents=True)
        (dep2 / "common.ivy").write_text("#lang ivy1.7\n# dep2 version\n")

        main = ws / "protocol-testing" / "main"
        main.mkdir(parents=True)
        (main / "entry.ivy").write_text("#lang ivy1.7\ninclude common\n")

        layers = [
            WorkspaceLayer(
                id="dep1", include_paths=["protocol-testing/dep1"], priority=1
            ),
            WorkspaceLayer(
                id="dep2", include_paths=["protocol-testing/dep2"], priority=2
            ),
            WorkspaceLayer(
                id="main",
                include_paths=["protocol-testing/main"],
                priority=3,
                depends_on=["dep1", "dep2"],
            ),
        ]
        resolver = IncludeResolver(str(ws), workspace_layers=layers)
        resolver.create_staging_directory()
        resolver.build_layered_staging()

        main_dir = resolver._partition_staging["main"]
        link_target = os.path.realpath(os.path.join(main_dir, "common.ivy"))
        # First dep listed (dep1) should win
        assert "dep1" in link_target


class TestSerializationRoundtripWithDependsOn:
    """Test that config dict preserves depends_on field."""

    def test_roundtrip(self, tmp_path):
        layers = [
            WorkspaceLayer(id="quic", include_paths=["quic"], priority=1),
            WorkspaceLayer(
                id="apt",
                include_paths=["apt"],
                priority=3,
                depends_on=["quic", "minip"],
            ),
        ]
        resolver = IncludeResolver(str(tmp_path), workspace_layers=layers)
        d = resolver.to_config_dict()

        # Check serialized form
        apt_layer = next(l for l in d["workspace_layers"] if l["id"] == "apt")
        assert apt_layer["depends_on"] == ["quic", "minip"]

        quic_layer = next(l for l in d["workspace_layers"] if l["id"] == "quic")
        assert quic_layer["depends_on"] == []

        # Restore and verify
        restored = IncludeResolver.from_config(d)
        restored_apt = next(l for l in restored._workspace_layers if l.id == "apt")
        assert restored_apt.depends_on == ["quic", "minip"]

        restored_quic = next(l for l in restored._workspace_layers if l.id == "quic")
        assert restored_quic.depends_on == []
