"""MCP tool for running Ivy tests against IUTs via PANTHER."""

from __future__ import annotations

import asyncio
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
    config = dict(_CONFIG_TEMPLATE)
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
    """Find the newest output directory created after start_time."""
    outputs_base = os.path.join(root, "outputs")
    if not os.path.isdir(outputs_base):
        return None

    candidates = []
    for entry in Path(outputs_base).iterdir():
        if entry.is_dir() and entry.stat().st_mtime >= start_time:
            candidates.append(entry)

    if not candidates:
        return None

    newest = max(candidates, key=lambda d: d.stat().st_mtime)
    return str(newest)


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


def register_iut_testing_tools(mcp: Any, ctx: Any) -> None:
    """Register IUT testing tools on the MCP server.

    Placeholder — will be populated in a follow-up commit.
    """
