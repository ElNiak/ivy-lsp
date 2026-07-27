"""Tests for ivy_manifest MCP tool and enhanced ivy_coverage output."""

import os
import tempfile

import pytest

from ivy_lsp.core.semantic.rfc_annotations import (
    find_manifests,
    load_manifest_with_metadata,
    validate_manifest,
)

SAMPLE_MANIFEST = """\
rfc: RFC9000
metadata:
  generated_at: "2026-03-17T12:00:00Z"
  generator_version: "ivy-lsp"
  source: "/tmp/test_rfc.txt"
  content_hash: "abc123"
requirements:
  rfc9000:4.1:
    text: "senders MUST NOT send data"
    section: "4.1"
    level: MUST
    layer: frame
    testable: true
  rfc9000:8.1:
    text: "SHOULD validate tokens"
    section: "8.1"
    level: SHOULD
    layer: connection
    testable: true
"""


class TestManifestInfoMode:
    """Test the building blocks used by ivy_manifest(mode='info')."""

    def test_find_manifests_discovers_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pt = os.path.join(tmpdir, "protocol-testing", "quic")
            os.makedirs(pt)
            manifest = os.path.join(pt, "quic_requirements.yaml")
            with open(manifest, "w") as f:
                f.write(SAMPLE_MANIFEST)

            results = find_manifests(tmpdir)
            assert len(results) == 1
            assert results[0] == manifest

    def test_load_manifest_with_metadata_info(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(SAMPLE_MANIFEST)
            path = f.name

        try:
            result = load_manifest_with_metadata(path)
            assert len(result.requirements) == 2
            assert result.metadata is not None
            assert result.metadata.generator_version == "ivy-lsp"
        finally:
            os.unlink(path)


class TestManifestValidateMode:
    """Test validation used by ivy_manifest(mode='validate')."""

    def test_valid_manifest(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        data = yaml.safe_load(SAMPLE_MANIFEST)
        warnings = validate_manifest(data)
        assert warnings == []

    def test_manifest_with_issues(self):
        data = {
            "requirements": {
                "rfc9000:4.1": {
                    "level": "INVALID",
                },
            },
        }
        warnings = validate_manifest(data)
        assert len(warnings) > 0
        assert any("rfc" in w.lower() for w in warnings)

    def test_manifest_missing_fields(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {},
            },
        }
        warnings = validate_manifest(data)
        assert any("text" in w for w in warnings)
        assert any("section" in w for w in warnings)


class TestMultipleProtocols:
    """Test multi-protocol manifest discovery."""

    def test_multiple_protocols_discovered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create manifests for two protocols
            for prot in ("quic", "tls"):
                pt = os.path.join(tmpdir, "protocol-testing", prot)
                os.makedirs(pt)
                with open(os.path.join(pt, f"{prot}_requirements.yaml"), "w") as f:
                    f.write(f"rfc: RFC_{prot}\nrequirements: {{}}\n")

            results = find_manifests(tmpdir)
            assert len(results) == 2
            paths_str = " ".join(results)
            assert "quic" in paths_str
            assert "tls" in paths_str

    def test_protocol_without_manifest_detected(self):
        """A protocol dir without *_requirements.yaml should be detectable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create quic with manifest
            quic_dir = os.path.join(tmpdir, "protocol-testing", "quic")
            os.makedirs(quic_dir)
            with open(os.path.join(quic_dir, "quic_requirements.yaml"), "w") as f:
                f.write("rfc: RFC9000\nrequirements: {}\n")

            # Create tls WITHOUT manifest
            tls_dir = os.path.join(tmpdir, "protocol-testing", "tls")
            os.makedirs(tls_dir)
            with open(os.path.join(tls_dir, "some_model.ivy"), "w") as f:
                f.write("# tls model\n")

            manifests = find_manifests(tmpdir)
            assert len(manifests) == 1

            # The tls dir exists but has no manifest
            pt_dir = os.path.join(tmpdir, "protocol-testing")
            protocols_with = set()
            for mpath in manifests:
                rel = os.path.relpath(mpath, tmpdir)
                parts = rel.split("protocol-testing/")[1].split("/")
                if parts:
                    protocols_with.add(parts[0])

            all_protocols = set()
            for entry in os.listdir(pt_dir):
                if os.path.isdir(os.path.join(pt_dir, entry)):
                    all_protocols.add(entry)

            without = all_protocols - protocols_with
            assert "tls" in without
            assert "quic" not in without
