"""Integration test: verify incremental T1/T2 re-analyzes only changed files."""

import pytest

from ivy_lsp.core.parsing.symbols import IncludeGraph
from ivy_lsp.core.semantic.model import SemanticModel
from ivy_lsp.core.semantic.nodes import TypeNode
from ivy_lsp.core.workspace.context import FileChange, StalenessInfo
from ivy_lsp.infra.utils.hashing import file_sha256
from ivy_lsp.lsp.bulk_orchestrator import _validate_changes, expand_with_budget


class TestIncrementalPipeline:
    """End-to-end test of the incremental staleness pipeline."""

    def test_single_leaf_change_reanalyzes_only_that_file(self, tmp_path):
        """Modify one leaf test file -> only it should be in the dirty set."""
        base = tmp_path / "base.ivy"
        base.write_text("#lang ivy1.7\ntype cid")
        leaf = tmp_path / "leaf.ivy"
        leaf.write_text("#lang ivy1.7\ninclude base")
        other = tmp_path / "other.ivy"
        other.write_text("#lang ivy1.7\ninclude base")

        graph = IncludeGraph()
        graph.add_edge(str(leaf), str(base))
        graph.add_edge(str(other), str(base))

        staleness = StalenessInfo(
            status="stale_minor",
            changed_files=1,
            total_files=3,
            file_changes=[FileChange("leaf.ivy", "modified", "old_sha")],
        )

        dirty = _validate_changes(staleness, str(tmp_path))
        assert dirty == {str(leaf)}

        expanded = expand_with_budget(dirty, graph, 3)
        assert expanded is not None
        assert expanded == {str(leaf)}

    def test_base_file_change_cascades_to_dependents(self, tmp_path):
        """Modify base.ivy -> cascades to leaf.ivy and other.ivy.

        total_files=10 keeps the 3-file cascade (30%) under the 50% budget.
        """
        base = tmp_path / "base.ivy"
        base.write_text("#lang ivy1.7\ntype cid")
        leaf = tmp_path / "leaf.ivy"
        leaf.write_text("#lang ivy1.7\ninclude base")
        other = tmp_path / "other.ivy"
        other.write_text("#lang ivy1.7\ninclude base")

        graph = IncludeGraph()
        graph.add_edge(str(leaf), str(base))
        graph.add_edge(str(other), str(base))

        staleness = StalenessInfo(
            status="stale_minor",
            changed_files=1,
            total_files=10,
            file_changes=[FileChange("base.ivy", "modified", "old_sha")],
        )

        dirty = _validate_changes(staleness, str(tmp_path))
        assert dirty == {str(base)}

        expanded = expand_with_budget(dirty, graph, 10)
        assert expanded is not None
        assert expanded == {str(base), str(leaf), str(other)}

    def test_git_checkout_mtime_noise_skips_reanalysis(self, tmp_path):
        """Mtime changed but content identical -> hash-clean, no re-analysis."""
        f = tmp_path / "a.ivy"
        f.write_text("#lang ivy1.7\ntype t")
        real_sha = file_sha256(str(f))

        staleness = StalenessInfo(
            status="stale_minor",
            changed_files=1,
            total_files=10,
            file_changes=[FileChange("a.ivy", "modified", real_sha)],
        )

        dirty = _validate_changes(staleness, str(tmp_path))
        assert dirty == set()

    def test_tracking_sets_populated_from_cache(self):
        """SemanticModel.files can be used to populate tracking sets."""
        model = SemanticModel()
        model.update_file(
            "/tmp/a.ivy",
            [
                TypeNode(
                    id="t1",
                    name="cid",
                    qualified_name="cid",
                    file="/tmp/a.ivy",
                    line=1,
                    tier="tier1",
                )
            ],
            [],
            "tier1",
        )
        model.update_file(
            "/tmp/b.ivy",
            [
                TypeNode(
                    id="t2",
                    name="aid",
                    qualified_name="aid",
                    file="/tmp/b.ivy",
                    line=1,
                    tier="tier1",
                )
            ],
            [],
            "tier1",
        )

        tier1_files = set()
        tier2_files = set()
        tier1_files.update(model.files)
        tier2_files.update(model.files)
        assert tier1_files == {"/tmp/a.ivy", "/tmp/b.ivy"}
        assert tier2_files == {"/tmp/a.ivy", "/tmp/b.ivy"}

        dirty_file = "/tmp/a.ivy"
        model.remove_file(dirty_file)
        tier1_files.discard(dirty_file)
        tier2_files.discard(dirty_file)

        assert tier1_files == {"/tmp/b.ivy"}
        assert tier2_files == {"/tmp/b.ivy"}
        assert model.files == {"/tmp/b.ivy"}
