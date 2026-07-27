"""Unit tests for ivy_iut_test MCP tool helpers."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from ivy_lsp.mcp.tools.iut_testing import (
    _build_experiment_config,
    _collect_iut_logs,
    _find_output_dir_by_timestamp,
    _find_output_dir_deterministic,
    _load_experiment_summary,
    _prepare_user_config,
    _refine_verdict,
    _validate_inputs,
)


class TestValidateInputs:
    def test_missing_protocol(self, tmp_path):
        assert (
            _validate_inputs("", "test", "iut", str(tmp_path)) == "protocol is required"
        )

    def test_missing_test_name(self, tmp_path):
        assert (
            _validate_inputs("bgp", "", "iut", str(tmp_path)) == "test_name is required"
        )

    def test_missing_iut_name(self, tmp_path):
        assert (
            _validate_inputs("bgp", "test", "", str(tmp_path)) == "iut_name is required"
        )

    def test_unknown_protocol(self, tmp_path):
        result = _validate_inputs("nonexistent", "test", "iut", str(tmp_path))
        assert "not found" in result
        assert "Known protocols" in result

    def test_unknown_iut(self, tmp_path):
        proto_dir = (
            tmp_path / "panther" / "plugins" / "protocols" / "client_server" / "bgp"
        )
        proto_dir.mkdir(parents=True)
        result = _validate_inputs("bgp", "test", "bad_iut", str(tmp_path))
        assert "IUT plugin 'bad_iut' not found" in result

    def test_valid_inputs(self, tmp_path):
        proto_dir = (
            tmp_path / "panther" / "plugins" / "protocols" / "client_server" / "bgp"
        )
        proto_dir.mkdir(parents=True)
        iut_dir = (
            tmp_path / "panther" / "plugins" / "services" / "iut" / "bgp" / "frr_bgp"
        )
        iut_dir.mkdir(parents=True)
        assert _validate_inputs("bgp", "test", "frr_bgp", str(tmp_path)) is None


class TestBuildExperimentConfig:
    def test_default_config(self):
        config = _build_experiment_config(
            protocol="bgp",
            test_name="bgp_speaker_test_join",
            iut_name="frr_bgp",
            version="",
            timeout=120,
            run_id="abc12345",
        )
        assert config["paths"]["output_dir"] == "outputs/ivy-iut-abc12345"
        test_entry = config["tests"][0]
        assert test_entry["services"]["iut"]["implementation"]["name"] == "frr_bgp"
        assert (
            test_entry["services"]["ivy_tester"]["implementation"]["test"]
            == "bgp_speaker_test_join"
        )
        assert "version" not in test_entry["services"]["iut"]["protocol"]

    def test_with_extra_params(self):
        config = _build_experiment_config(
            protocol="bgp",
            test_name="test",
            iut_name="frr_bgp",
            version="rfc4271",
            timeout=60,
            run_id="xyz",
            extra_params={"speaker_as": "99"},
        )
        test_entry = config["tests"][0]
        assert test_entry["services"]["iut"]["implementation"]["version_config"] == {
            "speaker_as": "99"
        }
        assert test_entry["services"]["ivy_tester"]["implementation"][
            "version_config"
        ] == {"speaker_as": "99"}
        assert test_entry["services"]["iut"]["protocol"]["version"] == "rfc4271"

    def test_deterministic_output_dir(self):
        config = _build_experiment_config(
            protocol="quic",
            test_name="t",
            iut_name="i",
            version="",
            timeout=60,
            run_id="deadbeef",
        )
        assert "deadbeef" in config["paths"]["output_dir"]


class TestPrepareUserConfig:
    def test_overrides_test_name_and_timeout(self, tmp_path):
        original = {
            "tests": [
                {
                    "name": "Original",
                    "services": {
                        "iut": {
                            "timeout": 30,
                            "implementation": {"name": "frr_bgp", "type": "iut"},
                        },
                        "ivy_tester": {
                            "timeout": 30,
                            "implementation": {
                                "name": "panther_ivy",
                                "type": "testers",
                                "test": "old_test",
                            },
                        },
                    },
                }
            ]
        }
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(original, f)

        result = _prepare_user_config(str(config_file), "new_test", 999)
        svc = result["tests"][0]["services"]
        assert svc["ivy_tester"]["implementation"]["test"] == "new_test"
        assert svc["ivy_tester"]["timeout"] == 999
        assert svc["iut"]["timeout"] == 999

    def test_raises_on_empty_tests(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump({"tests": []}, f)

        with pytest.raises(ValueError, match="No tests found"):
            _prepare_user_config(str(config_file), "test", 60)


class TestFindOutputDir:
    def test_deterministic_found(self, tmp_path):
        out_dir = tmp_path / "outputs" / "ivy-iut-abc123"
        out_dir.mkdir(parents=True)
        assert _find_output_dir_deterministic(str(tmp_path), "abc123") == str(out_dir)

    def test_deterministic_not_found(self, tmp_path):
        assert _find_output_dir_deterministic(str(tmp_path), "nope") is None

    def test_timestamp_finds_newest(self, tmp_path):
        import time as _time

        outputs = tmp_path / "outputs"
        outputs.mkdir()
        old_dir = outputs / "old-run"
        old_dir.mkdir()
        _time.sleep(0.05)
        start = _time.time()
        _time.sleep(0.05)
        new_dir = outputs / "new-run"
        new_dir.mkdir()

        result = _find_output_dir_by_timestamp(str(tmp_path), start)
        assert result == str(new_dir)

    def test_timestamp_no_outputs(self, tmp_path):
        assert _find_output_dir_by_timestamp(str(tmp_path), 0.0) is None


class TestLoadExperimentSummary:
    def test_loads_valid_json(self, tmp_path):
        summary = {"tests": {"results": [{"status": "passed"}]}}
        summary_file = tmp_path / "experiment_summary.json"
        summary_file.write_text(json.dumps(summary))
        result = _load_experiment_summary(str(tmp_path))
        assert result == summary

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _load_experiment_summary(str(tmp_path)) is None

    def test_returns_none_for_none_dir(self):
        assert _load_experiment_summary(None) is None


class TestCollectIutLogs:
    def test_collects_test_log(self, tmp_path):
        test_dir = tmp_path / "0_My_Test_"
        test_dir.mkdir()
        (test_dir / "test.log").write_text("some log output")
        result = _collect_iut_logs(str(tmp_path))
        assert "some log output" in result
        assert "0_My_Test_/test.log" in result

    def test_empty_dir(self, tmp_path):
        assert _collect_iut_logs(str(tmp_path)) == ""

    def test_none_dir(self):
        assert _collect_iut_logs(None) == ""

    def test_no_truncation_at_helper_level(self, tmp_path):
        test_dir = tmp_path / "0_Test_"
        test_dir.mkdir()
        (test_dir / "test.log").write_text("x" * 5000)
        result = _collect_iut_logs(str(tmp_path))
        assert len(result) > 3000


class TestRefineVerdict:
    def test_summary_passed_overrides_fail(self):
        summary = {"tests": {"results": [{"status": "passed"}]}}
        assert _refine_verdict("fail", summary) == "pass"

    def test_summary_failed_overrides_pass(self):
        summary = {"tests": {"results": [{"status": "failed"}]}}
        assert _refine_verdict("pass", summary) == "fail"

    def test_summary_timeout(self):
        summary = {"tests": {"results": [{"status": "timeout"}]}}
        assert _refine_verdict("fail", summary) == "timeout"

    def test_summary_unknown_keeps_subprocess(self):
        summary = {"tests": {"results": [{"status": "unknown"}]}}
        assert _refine_verdict("fail", summary) == "fail"

    def test_no_results_keeps_subprocess(self):
        assert _refine_verdict("pass", {"tests": {}}) == "pass"

    def test_empty_summary_keeps_subprocess(self):
        assert _refine_verdict("error", {}) == "error"


class MockToolContext:
    """Minimal mock for the MCP ToolContext."""

    def __init__(self, root: str):
        self.root = root


class TestIutTestTool:
    """Tests for the registered ivy_iut_test MCP tool."""

    def _make_tool(self, root: str):
        """Create the tool function by calling register_iut_testing_tools.

        Patches safe_tool to be a no-op so we test the tool logic directly
        without needing the full MCP infrastructure (sidecar, config, etc.).
        """
        from ivy_lsp.mcp.tools.iut_testing import register_iut_testing_tools

        mock_mcp = MagicMock()
        captured_fn = None

        def capture_tool():
            def decorator(fn):
                nonlocal captured_fn
                captured_fn = fn
                return fn

            return decorator

        mock_mcp.tool = capture_tool
        ctx = MockToolContext(root)

        def noop_safe_tool(**_kwargs):
            def passthrough(fn):
                return fn

            return passthrough

        with patch(
            "ivy_lsp.mcp.tools.iut_testing.safe_tool", noop_safe_tool, create=True
        ):
            with patch("ivy_lsp.mcp.tools.safe_tool", noop_safe_tool):
                register_iut_testing_tools(mock_mcp, ctx)
        return captured_fn

    def test_panther_not_found(self, tmp_path):
        tool = self._make_tool(str(tmp_path))
        with patch("shutil.which", return_value=None):
            result = asyncio.run(tool(protocol="bgp", test_name="t", iut_name="i"))
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_validation_failure(self, tmp_path):
        tool = self._make_tool(str(tmp_path))
        with patch("shutil.which", return_value="/usr/bin/panther"):
            result = asyncio.run(
                tool(protocol="nonexistent", test_name="t", iut_name="i")
            )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_timeout_verdict(self, tmp_path):
        proto = tmp_path / "panther" / "plugins" / "protocols" / "client_server" / "bgp"
        proto.mkdir(parents=True)
        iut = tmp_path / "panther" / "plugins" / "services" / "iut" / "bgp" / "frr_bgp"
        iut.mkdir(parents=True)

        tool = self._make_tool(str(tmp_path))

        async def mock_communicate():
            await asyncio.sleep(10)
            return b"", b""

        mock_proc = AsyncMock()
        mock_proc.communicate = mock_communicate
        mock_proc.kill = MagicMock()

        with patch("shutil.which", return_value="/usr/bin/panther"):
            with patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ):
                result = asyncio.run(
                    tool(
                        protocol="bgp",
                        test_name="test",
                        iut_name="frr_bgp",
                        timeout=1,
                    )
                )

        assert result["verdict"] == "timeout"
        assert result["error"] is not None
