"""Shared markdown building-block helpers for tool formatters."""

from __future__ import annotations

from typing import Any


def _section(title: str, level: int = 3) -> str:
    """Return a markdown section header."""
    return f"{'#' * level} {title}"


def _kv(key: str, value: Any) -> str:
    """Return a bold key-value line."""
    return f"**{key}**: {value}"


def _badge(label: str) -> str:
    """Return an inline badge like `[MUST]`."""
    return f"`{label}`"


def _pct_bar(pct: float, width: int = 20) -> str:
    """Return a text progress bar like [############........] 60.0%."""
    filled = int(round(pct / 100 * width))
    bar = "#" * filled + "." * (width - filled)
    return f"[{bar}] {pct:.1f}%"


def _diag_line(d: dict) -> str:
    """Format a single diagnostic as a bullet line."""
    sev = d.get("severity", "info")
    icon = {"error": "X", "warning": "!", "hint": "?", "info": "i"}.get(sev, "-")
    loc = d.get("file", "")
    line = d.get("line")
    if loc and line:
        loc = f"`{loc}:{line}`"
    elif loc:
        loc = f"`{loc}`"
    elif line:
        loc = f"line {line}"
    msg = d.get("message", "")
    parts = [f"[{icon}]"]
    if loc:
        parts.append(loc)
    parts.append(msg)
    return "- " + " ".join(parts)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    """Build a markdown table from headers and rows."""
    lines = ["| " + " | ".join(str(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _code_block(content: str, lang: str = "") -> str:
    """Wrap content in a fenced code block."""
    return f"```{lang}\n{content}\n```"


def _bullet_list(items: list[str], max_items: int = 30) -> str:
    """Return a bullet list, truncating if needed."""
    lines = [f"- `{item}`" for item in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"- ... and {len(items) - max_items} more")
    return "\n".join(lines)
