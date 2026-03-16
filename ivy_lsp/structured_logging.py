"""Structured logging primitives for the Ivy LSP server."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, MutableMapping, Optional, Tuple


class LogCategory(str, Enum):
    """Categories used to classify structured log events."""

    MILESTONE = "MIL"
    ACTIVITY = "ACT"
    DIAGNOSTIC = "DIA"
    PERFORMANCE = "PER"


@dataclass
class LogEvent:
    """A structured log event carrying a category, phase, and data payload."""

    category: LogCategory
    phase: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


class StructuredLogAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Logging adapter that formats messages with category and phase prefixes."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> Tuple[str, MutableMapping[str, Any]]:
        """Prepend category/phase prefix and append JSON data to msg."""
        extra = kwargs.get("extra", {})
        event: Optional[LogEvent] = extra.pop("event", None)
        if event is None:
            return msg, kwargs

        if event.phase:
            prefix = f"[{event.category.value}:{event.phase}]"
        else:
            prefix = f"[{event.category.value}]"

        if event.data:
            try:
                data_str = json.dumps(event.data, default=str)
            except (TypeError, ValueError):
                data_str = str(event.data)
            formatted = f"{prefix} {msg} | {data_str}"
        else:
            formatted = f"{prefix} {msg}"

        return formatted, kwargs
