"""Tests for staleness detection and manifest metadata loading."""

import os
import tempfile

import pytest

from ivy_lsp.rfc.staleness import check_staleness
from ivy_lsp.semantic.nodes import ManifestMetadata
from ivy_lsp.semantic.rfc_annotations import (
    ManifestLoadResult,
    load_manifest_with_metadata,
)


class TestManifestLoadResult:
    def test_load_valid_manifest_with_metadata(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        content = """\
rfc: RFC9000
metadata:
  generated_at: "2026-03-17T12:00:00Z"
  generator_version: "ivy-lsp"
  source: "https://www.rfc-editor.org/rfc/rfc9000.txt"
  content_hash: "abc123"
requirements:
  rfc9000:4.1:
    text: "senders MUST NOT send data"
    section: "4.1"
    level: MUST
    layer: frame
    testable: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_manifest_with_metadata(path)
            assert isinstance(result, ManifestLoadResult)
            assert len(result.requirements) == 1
            assert result.metadata is not None
            assert isinstance(result.metadata, ManifestMetadata)
            assert result.metadata.source.endswith("rfc9000.txt")
            assert result.metadata.content_hash == "abc123"
            assert result.metadata.generated_at == "2026-03-17T12:00:00Z"
            assert result.path == path
        finally:
            os.unlink(path)

    def test_load_manifest_without_metadata(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        content = """\
rfc: RFC9000
requirements:
  rfc9000:4.1:
    text: "something"
    section: "4.1"
    level: MUST
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_manifest_with_metadata(path)
            assert result.metadata is None
            assert len(result.requirements) == 1
        finally:
            os.unlink(path)

    def test_load_nonexistent_returns_warnings(self):
        result = load_manifest_with_metadata("/nonexistent/path.yaml")
        assert result.warnings
        assert result.requirements == {}

    def test_load_manifest_with_validation_warnings(self):
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        content = """\
requirements:
  rfc9000:4.1:
    text: "something"
    section: "4.1"
    level: INVALID_LEVEL
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            result = load_manifest_with_metadata(path)
            # Should have warnings about missing rfc and invalid level
            assert any("rfc" in w.lower() for w in result.warnings)
            assert any("invalid level" in w.lower() for w in result.warnings)
        finally:
            os.unlink(path)


class TestStalenessReport:
    @pytest.mark.asyncio
    async def test_no_metadata_info_message(self):
        report = await check_staleness(
            manifest_source="",
            manifest_hash="",
            rfc_number="",
            check_online=False,
        )
        assert not report.is_stale
        assert any("cannot check" in i.lower() for i in report.info)

    @pytest.mark.asyncio
    async def test_content_hash_match(self):
        """When source hash matches, manifest is not stale."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Some RFC content MUST be validated.")
            path = f.name

        try:
            from ivy_lsp.rfc.fetcher import _compute_hash

            expected_hash = _compute_hash("Some RFC content MUST be validated.")
            report = await check_staleness(
                manifest_source=path,
                manifest_hash=expected_hash,
                check_online=False,
            )
            assert not report.is_stale
            assert report.content_hash_match is True
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_content_hash_mismatch(self):
        """When source hash doesn't match, manifest is stale."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Updated content")
            path = f.name

        try:
            report = await check_staleness(
                manifest_source=path,
                manifest_hash="old_hash_that_doesnt_match",
                check_online=False,
            )
            assert report.is_stale
            assert report.content_hash_match is False
            assert any("changed" in r.lower() for r in report.reasons)
        finally:
            os.unlink(path)

    @pytest.mark.asyncio
    async def test_graceful_fetch_failure(self):
        """Network errors should not crash, just add info."""
        report = await check_staleness(
            manifest_source="/nonexistent/path.txt",
            manifest_hash="abc",
            check_online=False,
        )
        # Should gracefully handle the error
        assert any("could not" in i.lower() for i in report.info)
        # Not marked stale since we couldn't check
        assert not report.is_stale

    @pytest.mark.asyncio
    async def test_online_check_skipped(self):
        """check_online=False should skip RFC editor API."""
        report = await check_staleness(
            manifest_source="",
            manifest_hash="",
            rfc_number="9000",
            check_online=False,
        )
        assert not report.checked_online
