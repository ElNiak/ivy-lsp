"""Workflow state management tool: ivy_workflow_state.

Manages the active-workflow flag and build-state files that track
multi-session workflow progress for Ivy protocol models.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal

import yaml

from ivy_lsp.mcp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)

_STATE_DIR = ".panther-ivy"
_ACTIVE_WORKFLOW_FILE = "active-workflow"
_BUILD_STATE_FILE = "build-state.yaml"


def _resolve_protocol_dir(ctx: Any, protocol: str | None) -> str | None:
    """Resolve the protocol directory from explicit name or active workspace."""
    root = getattr(ctx, "root", None)
    if root is None:
        return None

    proto_testing = os.path.join(root, "protocol-testing")
    if not os.path.isdir(proto_testing):
        return None

    if protocol:
        candidate = os.path.join(proto_testing, protocol)
        return candidate if os.path.isdir(candidate) else None

    ws = getattr(ctx, "active_workspace", None)
    if ws is not None:
        group = getattr(ws, "active_group", None)
        if group:
            candidate = os.path.join(proto_testing, group)
            if os.path.isdir(candidate):
                return candidate

    return None


def _state_dir(protocol_dir: str) -> str:
    return os.path.join(protocol_dir, _STATE_DIR)


def _ensure_state_dir(protocol_dir: str) -> str:
    path = _state_dir(protocol_dir)
    os.makedirs(path, exist_ok=True)
    return path


def register_workflow_state_tools(mcp: Any, ctx: Any) -> None:
    """Register workflow state management MCP tools."""

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_workflow_state(
        action: Literal["set", "get", "clear", "get_build", "set_build"],
        workflow: str | None = None,
        phase: str | None = None,
        protocol: str | None = None,
        caller: str | None = None,
        invocation_depth: int = 0,
        state: str | None = None,
    ) -> dict:
        """Manage workflow state files for multi-session build tracking.

        Controls the active-workflow flag and build-state persistence
        under ``<protocol_dir>/.panther-ivy/``.

        Args:
            action: One of "set", "get", "clear", "get_build", "set_build".
            workflow: For action="set": workflow name (e.g. "build", "verify").
            phase: For action="set": current phase within the workflow.
            protocol: Protocol name (e.g. "bgp", "quic"). Falls back to
                active workspace if omitted.
            caller: For action="set": identifier of the invoking workflow.
            invocation_depth: For action="set": nesting depth (default 0).
            state: For action="set_build": JSON-encoded build state dict.
        """
        if action == "set":
            return _handle_set(ctx, protocol, workflow, phase, caller, invocation_depth)
        elif action == "get":
            return _handle_get(ctx, protocol)
        elif action == "clear":
            return _handle_clear(ctx, protocol)
        elif action == "get_build":
            return _handle_get_build(ctx, protocol)
        elif action == "set_build":
            return _handle_set_build(ctx, protocol, state)
        else:
            return error_response(
                f"Unknown action '{action}'. "
                "Valid: set, get, clear, get_build, set_build."
            )


def _handle_set(
    ctx: Any,
    protocol: str | None,
    workflow: str | None,
    phase: str | None,
    caller: str | None,
    invocation_depth: int,
) -> dict:
    if not workflow:
        return error_response("action='set' requires 'workflow' parameter.")
    if not phase:
        return error_response("action='set' requires 'phase' parameter.")

    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return error_response(
            "Cannot resolve protocol directory. "
            "Provide 'protocol' parameter or set an active workspace."
        )

    state_path = _ensure_state_dir(protocol_dir)
    data: dict[str, Any] = {
        "workflow": workflow,
        "phase": phase,
        "invocation_depth": invocation_depth,
        "started": datetime.now(timezone.utc).isoformat(),
    }
    if caller is not None:
        data["caller"] = caller

    filepath = os.path.join(state_path, _ACTIVE_WORKFLOW_FILE)
    with open(filepath, "w") as f:
        yaml.safe_dump(data, f)

    logger.info("Workflow state set: %s/%s in %s", workflow, phase, protocol_dir)
    return {"success": True, "action": "set", "protocol_dir": protocol_dir, **data}


def _handle_get(ctx: Any, protocol: str | None) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return {
            "success": True,
            "action": "get",
            "active": False,
            "workflow": None,
            "phase": None,
            "message": "No protocol directory resolved.",
        }

    filepath = os.path.join(_state_dir(protocol_dir), _ACTIVE_WORKFLOW_FILE)
    if not os.path.exists(filepath):
        return {
            "success": True,
            "action": "get",
            "active": False,
            "workflow": None,
            "phase": None,
            "protocol_dir": protocol_dir,
        }

    try:
        with open(filepath) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, yaml.YAMLError):
        data = {}

    return {
        "success": True,
        "action": "get",
        "active": bool(data.get("workflow")),
        "protocol_dir": protocol_dir,
        **data,
    }


def _handle_clear(ctx: Any, protocol: str | None) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return error_response(
            "Cannot resolve protocol directory. "
            "Provide 'protocol' parameter or set an active workspace."
        )

    filepath = os.path.join(_state_dir(protocol_dir), _ACTIVE_WORKFLOW_FILE)
    try:
        os.unlink(filepath)
    except FileNotFoundError:
        pass

    logger.info("Workflow state cleared in %s", protocol_dir)
    return {"success": True, "action": "clear", "protocol_dir": protocol_dir}


def _handle_get_build(ctx: Any, protocol: str | None) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return {
            "success": True,
            "action": "get_build",
            "has_build": False,
            "state": None,
            "message": "No protocol directory resolved.",
        }

    filepath = os.path.join(_state_dir(protocol_dir), _BUILD_STATE_FILE)
    if not os.path.exists(filepath):
        return {
            "success": True,
            "action": "get_build",
            "has_build": False,
            "state": None,
            "protocol_dir": protocol_dir,
        }

    try:
        with open(filepath) as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        data = None

    return {
        "success": True,
        "action": "get_build",
        "has_build": data is not None,
        "state": data,
        "protocol_dir": protocol_dir,
    }


def _handle_set_build(ctx: Any, protocol: str | None, state_json: str | None) -> dict:
    if not state_json:
        return error_response(
            "action='set_build' requires 'state' parameter (JSON string)."
        )

    try:
        state_dict = json.loads(state_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return error_response(f"Invalid JSON in 'state' parameter: {exc}")

    if not isinstance(state_dict, dict):
        return error_response("'state' must be a JSON object.")

    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return error_response(
            "Cannot resolve protocol directory. "
            "Provide 'protocol' parameter or set an active workspace."
        )

    state_path = _ensure_state_dir(protocol_dir)
    filepath = os.path.join(state_path, _BUILD_STATE_FILE)
    with open(filepath, "w") as f:
        yaml.safe_dump(state_dict, f)

    logger.info("Build state written in %s", protocol_dir)
    return {
        "success": True,
        "action": "set_build",
        "protocol_dir": protocol_dir,
        "state": state_dict,
    }
