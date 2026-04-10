"""MCP tool for running Ivy tests against IUTs via PANTHER."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_TEMPLATE = {
    "logging": {"level": "INFO"},
    "paths": {"output_dir": "outputs", "plugin_dir": "panther/plugins"},
    "docker": {"force_build_docker_image": False, "use_buildx": True},
}


def register_iut_testing_tools(mcp: Any, ctx: Any) -> None:
    """Register IUT testing tools on the MCP server."""

    @mcp.tool()
    async def ivy_iut_test(
        protocol: str,
        test_name: str,
        iut_name: str,
        version: str = "",
        timeout: int = 120,
        extra_params: dict | None = None,
    ) -> dict[str, Any]:
        """Run an Ivy test against an IUT via PANTHER's experiment pipeline.

        Generates a temporary experiment config and invokes `panther run`.

        Args:
            protocol: Protocol name (e.g., "bgp", "quic").
            test_name: Ivy test file name without .ivy extension.
            iut_name: Registered IUT plugin name (e.g., "frr_bgp").
            version: Protocol version (default: protocol's default version).
            timeout: Total timeout in seconds (default: 120).
            extra_params: Override version_config values.
        """
        if not shutil.which("panther"):
            return {
                "success": False,
                "error": "panther CLI not found on PATH. Install PANTHER first.",
            }

        version = version or ""
        config = _build_experiment_config(
            protocol=protocol,
            test_name=test_name,
            iut_name=iut_name,
            version=version,
            timeout=timeout,
        )

        run_id = str(uuid.uuid4())[:8]
        tmp_dir = os.path.join(tempfile.gettempdir(), f"ivy-iut-{run_id}")
        os.makedirs(tmp_dir, exist_ok=True)
        config_path = os.path.join(tmp_dir, "config.yaml")

        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        t0 = time.monotonic()
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "panther",
                "run",
                "--config",
                config_path,
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

        return {
            "verdict": verdict,
            "test_name": test_name,
            "iut_name": iut_name,
            "protocol": protocol,
            "test_stdout": stdout[-5000:],
            "test_stderr": stderr[-2000:],
            "duration_seconds": round(duration, 2),
            "error": stderr if verdict in ("error", "timeout") else None,
        }


def _build_experiment_config(
    protocol: str,
    test_name: str,
    iut_name: str,
    version: str,
    timeout: int,
) -> dict:
    """Build a PANTHER experiment config dict."""
    config = dict(_CONFIG_TEMPLATE)
    config["tests"] = [
        {
            "name": f"IUT Test: {test_name} vs {iut_name}",
            "network_environment": {"type": "docker_compose"},
            "iterations": 1,
            "services": {
                "iut": {
                    "name": "iut",
                    "timeout": timeout,
                    "implementation": {
                        "name": iut_name,
                        "type": "iut",
                    },
                    "protocol": {
                        "name": protocol,
                        **({"version": version} if version else {}),
                        "role": "server",
                    },
                },
                "ivy_tester": {
                    "name": "ivy_tester",
                    "timeout": timeout,
                    "implementation": {
                        "name": "panther_ivy",
                        "type": "testers",
                        "test": test_name,
                    },
                    "protocol": {
                        "name": protocol,
                        **({"version": version} if version else {}),
                        "role": "client",
                        "target": "iut",
                    },
                },
            },
        }
    ]
    return config
