"""MCP tool for running Ivy tests against IUTs via PANTHER."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_TEMPLATE = {
    "logging": {"level": "INFO"},
    "docker": {"force_build_docker_image": False, "use_buildx": True},
}

_KNOWN_PROTOCOLS = {"quic", "bgp", "coap", "minip"}


def _validate_inputs(
    protocol: str, test_name: str, iut_name: str, root: str
) -> str | None:
    """Return an error message if inputs are invalid, None if valid."""
    if not protocol or not protocol.strip():
        return "protocol is required"
    if not test_name or not test_name.strip():
        return "test_name is required"
    if not iut_name or not iut_name.strip():
        return "iut_name is required"

    protocol = protocol.strip().lower()

    protocol_dir = os.path.join(
        root, "panther", "plugins", "protocols", "client_server", protocol
    )
    if not os.path.isdir(protocol_dir):
        hint = (
            f" Known protocols: {', '.join(sorted(_KNOWN_PROTOCOLS))}"
            if _KNOWN_PROTOCOLS
            else ""
        )
        return f"Protocol '{protocol}' not found at {protocol_dir}.{hint}"

    iut_dir = os.path.join(
        root, "panther", "plugins", "services", "iut", protocol, iut_name
    )
    if not os.path.isdir(iut_dir):
        return f"IUT plugin '{iut_name}' not found at {iut_dir}"

    return None


def _build_experiment_config(
    protocol: str,
    test_name: str,
    iut_name: str,
    version: str,
    timeout: int,
    run_id: str,
    extra_params: dict | None = None,
) -> dict:
    """Build a PANTHER experiment config dict with deterministic output dir."""
    config = copy.deepcopy(_CONFIG_TEMPLATE)
    config["paths"] = {
        "output_dir": f"outputs/ivy-iut-{run_id}",
        "plugin_dir": "panther/plugins",
    }

    iut_impl: dict[str, Any] = {"name": iut_name, "type": "iut"}
    tester_impl: dict[str, Any] = {
        "name": "panther_ivy",
        "type": "testers",
        "test": test_name,
    }

    if extra_params:
        iut_impl["version_config"] = extra_params
        tester_impl["version_config"] = extra_params

    iut_protocol: dict[str, Any] = {"name": protocol, "role": "server"}
    tester_protocol: dict[str, Any] = {
        "name": protocol,
        "role": "client",
        "target": "iut",
    }
    if version:
        iut_protocol["version"] = version
        tester_protocol["version"] = version

    config["tests"] = [
        {
            "name": f"IUT Test: {test_name} vs {iut_name}",
            "network_environment": {"type": "docker_compose"},
            "iterations": 1,
            "services": {
                "iut": {
                    "name": "iut",
                    "timeout": timeout,
                    "implementation": iut_impl,
                    "protocol": iut_protocol,
                },
                "ivy_tester": {
                    "name": "ivy_tester",
                    "timeout": timeout,
                    "implementation": tester_impl,
                    "protocol": tester_protocol,
                },
            },
        }
    ]
    return config


def _prepare_user_config(config_path: str, test_name: str, timeout: int) -> dict:
    """Load a user-provided config and override test_name and timeout."""
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path) as f:
        config = yaml.safe_load(f)

    tests = config.get("tests", [])
    if not tests:
        raise ValueError(f"No tests found in config at {config_path}")

    test_entry = tests[0]
    services = test_entry.get("services", {})
    for svc in services.values():
        svc["timeout"] = timeout
        impl = svc.get("implementation", {})
        if impl.get("type") == "testers":
            impl["test"] = test_name

    return config


def _find_output_dir_deterministic(root: str, run_id: str) -> str | None:
    """Find the output directory by deterministic run_id path."""
    candidate = os.path.join(root, "outputs", f"ivy-iut-{run_id}")
    return candidate if os.path.isdir(candidate) else None


def _find_output_dir_by_timestamp(root: str, start_time: float) -> str | None:
    """Find the newest output directory created after start_time.

    Args:
        root: Project root directory.
        start_time: Wall-clock timestamp from time.time(), compared against
            filesystem st_mtime. Do NOT pass time.monotonic().
    """
    outputs_base = os.path.join(root, "outputs")
    if not os.path.isdir(outputs_base):
        return None

    candidates: list[tuple[Path, float]] = []
    for entry in Path(outputs_base).iterdir():
        if not entry.is_dir():
            continue
        mtime = entry.stat().st_mtime
        if mtime >= start_time:
            candidates.append((entry, mtime))

    if not candidates:
        return None

    newest = max(candidates, key=lambda pair: pair[1])
    return str(newest[0])


def _load_experiment_summary(output_dir: str | None) -> dict | None:
    """Load experiment_summary.json from the output directory."""
    if not output_dir:
        return None
    summary_path = os.path.join(output_dir, "experiment_summary.json")
    if not os.path.isfile(summary_path):
        return None
    try:
        with open(summary_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to parse experiment_summary.json: %s", exc)
        return None


def _collect_iut_logs(output_dir: str | None) -> str:
    """Collect test.log content from test subdirectories."""
    if not output_dir or not os.path.isdir(output_dir):
        return ""

    logs: list[str] = []
    for entry in sorted(Path(output_dir).iterdir()):
        if not entry.is_dir():
            continue
        test_log = entry / "test.log"
        if test_log.is_file():
            try:
                content = test_log.read_text(errors="replace")
                logs.append(f"--- {entry.name}/test.log ---\n{content}")
            except OSError:
                pass

    return "\n".join(logs)


def _refine_verdict(subprocess_verdict: str, summary: dict) -> str:
    """Refine verdict using experiment_summary.json per-test status."""
    results = summary.get("tests", {}).get("results", [])
    if not results:
        return subprocess_verdict

    status = results[0].get("status", "").lower()
    if status == "passed":
        return "pass"
    if status == "failed":
        return "fail"
    if status == "timeout":
        return "timeout"
    return subprocess_verdict


def _resolve_panther_bin(workspace_root: str) -> str | None:
    """Walk up from *workspace_root* looking for ``.venv/bin/panther``.

    Returns the absolute path to the binary, or ``None`` if not found
    within 8 ancestor directories.
    """
    candidate = os.path.realpath(workspace_root)
    for _ in range(8):
        panther_bin = os.path.join(candidate, ".venv", "bin", "panther")
        if os.path.isfile(panther_bin) and os.access(panther_bin, os.X_OK):
            return panther_bin
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    return None


def register_iut_testing_tools(mcp: Any, ctx: Any) -> None:
    """Register IUT testing tools on the MCP server."""
    from ivy_lsp.mcp.tools import safe_tool

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_iut_test(
        protocol: str,
        test_name: str,
        iut_name: str,
        version: str = "",
        timeout: int = 120,
        extra_params: dict | None = None,
        config_path: str | None = None,
    ) -> dict[str, Any]:
        """Runs an Ivy test against an IUT via PANTHER's experiment pipeline.

        Returns: {verdict: pass|fail|timeout, test_name, iut_name, logs: str, duration_s, config_path — generated experiment config}

        Run ivy_compile first to verify the test compiles. Use ivy_diagnostics mode=full to check for model issues before testing.

        IMPORTANT: spawns Docker containers; takes 30s-3min. Requires PANTHER framework installed.

        Args:
            protocol: Protocol name (e.g., "bgp", "quic").
            test_name: Ivy test file name without .ivy extension.
            iut_name: Registered IUT plugin name (e.g., "frr_bgp").
            version: Protocol version (default: protocol's default version).
            timeout: Total timeout in seconds (default: 120).
            extra_params: Override version_config values.
            config_path: Path to existing experiment config YAML. Overrides
                         generated config when provided.
        """
        panther_bin = _resolve_panther_bin(ctx.root) or shutil.which("panther")
        if not panther_bin:
            return {
                "success": False,
                "error": (
                    "panther CLI not found. Looked for .venv/bin/panther "
                    "in ancestor directories of "
                    f"{ctx.root} and on PATH."
                ),
            }

        run_id = str(uuid.uuid4())[:8]
        using_user_config = config_path is not None

        if not using_user_config:
            validation_error = _validate_inputs(protocol, test_name, iut_name, ctx.root)
            if validation_error:
                return {"success": False, "error": validation_error}

        tmp_dir = os.path.join(tempfile.gettempdir(), f"ivy-iut-{run_id}")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            if using_user_config:
                config = _prepare_user_config(config_path, test_name, timeout)
            else:
                version = version or ""
                config = _build_experiment_config(
                    protocol=protocol,
                    test_name=test_name,
                    iut_name=iut_name,
                    version=version,
                    timeout=timeout,
                    run_id=run_id,
                    extra_params=extra_params,
                )

            final_config_path = os.path.join(tmp_dir, "config.yaml")
            with open(final_config_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False)
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {"success": False, "error": f"Config error: {exc}"}

        wall_start = time.time()
        t0 = time.monotonic()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                panther_bin,
                "run",
                "--config",
                final_config_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ctx.root,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            duration = time.monotonic() - t0
            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")
            verdict = "pass" if proc.returncode == 0 else "fail"

        except asyncio.TimeoutError:
            duration = time.monotonic() - t0
            if proc is not None:
                proc.kill()
                await proc.wait()
            stdout = ""
            stderr = "Timeout exceeded"
            verdict = "timeout"
        except Exception as exc:
            duration = time.monotonic() - t0
            stdout = ""
            stderr = str(exc)
            verdict = "error"
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        if not using_user_config:
            output_dir = _find_output_dir_deterministic(ctx.root, run_id)
        else:
            output_dir = _find_output_dir_by_timestamp(ctx.root, wall_start)

        summary = _load_experiment_summary(output_dir)
        iut_logs = _collect_iut_logs(output_dir)

        if summary:
            verdict = _refine_verdict(verdict, summary)

        return {
            "verdict": verdict,
            "test_name": test_name,
            "iut_name": iut_name,
            "protocol": protocol,
            "test_stdout": stdout[-5000:],
            "test_stderr": stderr[-2000:],
            "iut_logs": iut_logs[-3000:] if iut_logs else "",
            "duration_seconds": round(duration, 2),
            "output_dir": output_dir or "",
            "experiment_summary": summary,
            "error": stderr if verdict in ("error", "timeout") else None,
        }
