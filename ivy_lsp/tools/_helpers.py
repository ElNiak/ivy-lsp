"""Shared helpers for MCP tool modules."""

from __future__ import annotations

import json


def error_response(message: str) -> str:
    """Return a JSON error response string."""
    return json.dumps({"success": False, "message": message})
