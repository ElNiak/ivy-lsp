"""Tests for Z3DIR injection and workspace file staging in run_ivy_compile."""

import os
from pathlib import Path

from ivy_lsp.core.verification import _find_workspace_root, _stage_workspace_files


class TestFindWorkspaceRoot:
    def test_finds_ivyworkspace_in_parent(self, tmp_path):
        ws_root = tmp_path / "protocol-testing" / "bgp"
        ws_root.mkdir(parents=True)
        (ws_root / ".ivyworkspace").write_text("version: 3")
        test_file = ws_root / "bgp_tests" / "speaker_tests" / "test.ivy"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("#lang ivy1.7")
        assert _find_workspace_root(str(test_file)) == str(ws_root)

    def test_returns_none_when_no_workspace(self, tmp_path):
        test_file = tmp_path / "test.ivy"
        test_file.write_text("#lang ivy1.7")
        assert _find_workspace_root(str(test_file)) is None


class TestStageWorkspaceFiles:
    def test_copies_ivy_files_from_workspace(self, tmp_path):
        ws_root = tmp_path / "bgp"
        (ws_root / "bgp_stack").mkdir(parents=True)
        (ws_root / "bgp_stack" / "bgp_fsm.ivy").write_text("#lang ivy1.7\n# fsm")
        (ws_root / "bgp_utils").mkdir(parents=True)
        (ws_root / "bgp_utils" / "bgp_type.ivy").write_text("#lang ivy1.7\n# type")
        include_dir = tmp_path / "include" / "1.7"
        include_dir.mkdir(parents=True)
        staged = _stage_workspace_files(str(ws_root), str(include_dir))
        assert (include_dir / "bgp_fsm.ivy").exists()
        assert (include_dir / "bgp_type.ivy").exists()
        assert len(staged) == 2

    def test_cleanup_removes_staged_files(self, tmp_path):
        ws_root = tmp_path / "bgp"
        (ws_root / "stack").mkdir(parents=True)
        (ws_root / "stack" / "a.ivy").write_text("#lang ivy1.7")
        include_dir = tmp_path / "include" / "1.7"
        include_dir.mkdir(parents=True)
        (include_dir / "existing.ivy").write_text("# keep me")
        staged = _stage_workspace_files(str(ws_root), str(include_dir))
        for f in staged:
            os.remove(f)
        assert not (include_dir / "a.ivy").exists()
        assert (include_dir / "existing.ivy").exists()
