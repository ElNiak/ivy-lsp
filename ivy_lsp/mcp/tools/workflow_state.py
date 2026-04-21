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
_JOURNAL_FILE = "workflow-journal.yaml"
_JOURNAL_ARCHIVE_DIR = "journal-archive"

_VALID_EVENT_TYPES = frozenset(
    {
        "session_start",
        "session_end",
        "decision",
        "phase_transition",
        "progress",
        "error",
        "context_switch",
        "gate_dispatched",
        "gate_verdict",
        "plan_approved",
        "workflow_resumed",
        "knowledge_captured",
    }
)


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


def _read_active_state(state_path: str) -> dict | None:
    """Read and parse the active-workflow YAML file."""
    filepath = os.path.join(state_path, _ACTIVE_WORKFLOW_FILE)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except (OSError, yaml.YAMLError):
        return None


def _read_journal(state_path: str) -> list[dict]:
    """Read the journal YAML file, returning an empty list on missing/corrupt."""
    filepath = os.path.join(state_path, _JOURNAL_FILE)
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath) as f:
            loaded = yaml.safe_load(f)
        return loaded if isinstance(loaded, list) else []
    except (OSError, yaml.YAMLError):
        return []


def _write_journal(state_path: str, entries: list[dict]) -> None:
    """Write the journal YAML file."""
    filepath = os.path.join(state_path, _JOURNAL_FILE)
    with open(filepath, "w") as f:
        yaml.safe_dump(entries, f, default_flow_style=False)


def register_workflow_state_tools(mcp: Any, ctx: Any) -> None:
    """Register workflow state management MCP tools."""

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_workflow_state(
        action: Literal[
            "set",
            "get",
            "clear",
            "get_build",
            "set_build",
            "append_journal",
            "get_journal",
        ],
        workflow: str | None = None,
        phase: str | None = None,
        protocol: str | None = None,
        caller: str | None = None,
        invocation_depth: int = 0,
        state: str | dict | None = None,
        event_type: str | None = None,
        last_n: int = 20,
    ) -> dict:
        """Manages workflow state files for multi-session build tracking.

        Responses are trimmed to novel information only (no echo of input
        parameters). Success paths return ``{"success": True, ...}``; failures
        go through ``error_response()``.

        Modes (return shapes):
        - set: {success, protocol_dir, started, phase_transition}
        - get: {success, active, workflow, phase, protocol_dir, ...}
        - clear: {success, protocol_dir}
        - get_build: {success, has_build, state, protocol_dir}
        - set_build: {success, protocol_dir}
        - append_journal: {success, protocol_dir, ts, journal_size, rotated}
        - get_journal: {success, entries, count, total, protocol_dir}

        Use set at workflow start, append_journal for progress events,
        get_journal to resume after session break.

        Args:
            action: One of "set", "get", "clear", "get_build", "set_build",
                "append_journal", "get_journal".
            workflow: For action="set": workflow name (e.g. "build", "verify").
            phase: For action="set": current phase within the workflow.
            protocol: Protocol name (e.g. "bgp", "quic"). Falls back to
                active workspace if omitted.
            caller: For action="set": identifier of the invoking workflow.
            invocation_depth: For action="set": nesting depth (default 0).
            state: For action="set_build": JSON-encoded build state dict.
                For action="append_journal": JSON-encoded event payload dict.
            event_type: For action="append_journal": event type
                (e.g. "decision", "error", "progress").
            last_n: For action="get_journal": number of recent entries
                to return (default 20).
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
        elif action == "append_journal":
            return _handle_append_journal(ctx, protocol, event_type, state)
        elif action == "get_journal":
            return _handle_get_journal(ctx, protocol, last_n)
        else:
            return error_response(
                f"Unknown action '{action}'. Valid: set, get, clear, "
                "get_build, set_build, append_journal, get_journal."
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

    prev_state = _read_active_state(state_path)
    previous_phase = prev_state.get("phase") if prev_state else None

    started = datetime.now(timezone.utc).isoformat()
    data: dict[str, Any] = {
        "workflow": workflow,
        "phase": phase,
        "invocation_depth": invocation_depth,
        "started": started,
    }
    if caller is not None:
        data["caller"] = caller

    filepath = os.path.join(state_path, _ACTIVE_WORKFLOW_FILE)
    with open(filepath, "w") as f:
        yaml.safe_dump(data, f)

    phase_transition: dict[str, str] | None = None
    if previous_phase and previous_phase != phase:
        phase_transition = {"from": previous_phase, "to": phase}
        entries = _read_journal(state_path)
        entries.append(
            {
                "ts": started,
                "type": "phase_transition",
                "workflow": workflow,
                "phase": phase,
                "payload": phase_transition,
            }
        )
        _write_journal(state_path, entries)

    logger.info("Workflow state set: %s/%s in %s", workflow, phase, protocol_dir)
    return {
        "success": True,
        "protocol_dir": protocol_dir,
        "started": started,
        "phase_transition": phase_transition,
    }


def _handle_get(ctx: Any, protocol: str | None) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return {
            "success": True,
            "active": False,
            "workflow": None,
            "phase": None,
            "message": "No protocol directory resolved.",
        }

    filepath = os.path.join(_state_dir(protocol_dir), _ACTIVE_WORKFLOW_FILE)
    if not os.path.exists(filepath):
        return {
            "success": True,
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
    return {"success": True, "protocol_dir": protocol_dir}


def _handle_get_build(ctx: Any, protocol: str | None) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return {
            "success": True,
            "has_build": False,
            "state": None,
            "message": "No protocol directory resolved.",
        }

    filepath = os.path.join(_state_dir(protocol_dir), _BUILD_STATE_FILE)
    if not os.path.exists(filepath):
        return {
            "success": True,
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
        "has_build": data is not None,
        "state": data,
        "protocol_dir": protocol_dir,
    }


def _handle_set_build(
    ctx: Any, protocol: str | None, state_json: str | dict | None
) -> dict:
    if not state_json:
        return error_response(
            "action='set_build' requires 'state' parameter (JSON string)."
        )

    if isinstance(state_json, dict):
        state_dict = state_json
    else:
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
        "protocol_dir": protocol_dir,
    }


def _handle_append_journal(
    ctx: Any,
    protocol: str | None,
    event_type: str | None,
    payload_json: str | dict | None,
) -> dict:
    if not event_type:
        return error_response(
            "action='append_journal' requires 'event_type' parameter."
        )
    if event_type not in _VALID_EVENT_TYPES:
        return error_response(
            f"Invalid event_type '{event_type}'. "
            f"Valid: {', '.join(sorted(_VALID_EVENT_TYPES))}."
        )

    payload: dict = {}
    if payload_json:
        if isinstance(payload_json, dict):
            payload = payload_json
        else:
            try:
                payload = json.loads(payload_json)
            except (json.JSONDecodeError, TypeError) as exc:
                return error_response(
                    f"Invalid JSON in 'state' parameter (event payload): {exc}"
                )
            if not isinstance(payload, dict):
                return error_response(
                    "'state' parameter (event payload) must be a JSON object."
                )

    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return error_response(
            "Cannot resolve protocol directory. "
            "Provide 'protocol' parameter or set an active workspace."
        )

    state_path = _ensure_state_dir(protocol_dir)

    active = _read_active_state(state_path)
    workflow = active.get("workflow") if active else None
    phase = active.get("phase") if active else None

    ts = datetime.now(timezone.utc).isoformat()
    entries = _read_journal(state_path)
    entries.append(
        {
            "ts": ts,
            "type": event_type,
            "workflow": workflow,
            "phase": phase,
            "payload": payload,
        }
    )
    _write_journal(state_path, entries)

    rotated = False
    journal_size = len(entries)
    if journal_size > 200:
        _rotate_journal(state_path, entries)
        rotated = True
        journal_size -= journal_size // 2

    logger.info("Journal event appended: %s in %s", event_type, protocol_dir)
    return {
        "success": True,
        "protocol_dir": protocol_dir,
        "ts": ts,
        "journal_size": journal_size,
        "rotated": rotated,
    }


def _rotate_journal(state_path: str, entries: list[dict]) -> None:
    """Archive oldest half of journal entries."""
    split_at = len(entries) // 2
    archive_entries = entries[:split_at]
    keep_entries = entries[split_at:]

    archive_dir = os.path.join(state_path, _JOURNAL_ARCHIVE_DIR)
    os.makedirs(archive_dir, exist_ok=True)
    archive_name = datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".yaml"
    archive_path = os.path.join(archive_dir, archive_name)

    existing: list[dict] = []
    if os.path.exists(archive_path):
        try:
            with open(archive_path) as f:
                loaded = yaml.safe_load(f)
                if isinstance(loaded, list):
                    existing = loaded
        except (OSError, yaml.YAMLError):
            pass

    with open(archive_path, "w") as f:
        yaml.safe_dump(existing + archive_entries, f, default_flow_style=False)

    _write_journal(state_path, keep_entries)


def _handle_get_journal(
    ctx: Any,
    protocol: str | None,
    last_n: int = 20,
) -> dict:
    protocol_dir = _resolve_protocol_dir(ctx, protocol)
    if protocol_dir is None:
        return {
            "success": True,
            "entries": [],
            "message": "No protocol directory resolved.",
        }

    entries = _read_journal(_state_dir(protocol_dir))
    result_entries = entries[-last_n:] if last_n < len(entries) else entries
    return {
        "success": True,
        "entries": result_entries,
        "count": len(result_entries),
        "total": len(entries),
        "protocol_dir": protocol_dir,
    }
