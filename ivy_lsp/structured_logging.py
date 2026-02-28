"""Structured logging primitives for the Ivy LSP server."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, MutableMapping, Optional, Tuple


class LogCategory(str, Enum):
    MILESTONE = "MIL"
    ACTIVITY = "ACT"
    DIAGNOSTIC = "DIA"
    PERFORMANCE = "PER"


@dataclass
class LogEvent:
    category: LogCategory
    phase: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


class StructuredLogAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> Tuple[str, MutableMapping[str, Any]]:
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
