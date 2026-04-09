"""Shared compiled regex patterns for Ivy language syntax.

Consolidates patterns previously duplicated across diagnostics/compute.py,
navigation/definition.py, and mcp/tools/verification.py.
"""

from __future__ import annotations

import re

INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)", re.MULTILINE)

ASSERTION_RE = re.compile(r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE)

BRACKET_TAG_RE = re.compile(r"#\s*\[")

EXPORT_ACTION_RE = re.compile(r"^\s*export\s+action\s+([\w.]+)", re.MULTILINE)

MONITOR_RE = re.compile(r"^\s*(?:before|after|around)\s+([\w.]+)", re.MULTILINE)
