"""Shared Ivy CLI parameter validation."""

from __future__ import annotations

import re

_VALID_IVY_PARAM = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")


def validate_ivy_param(value: str) -> str:
    """Validate an Ivy CLI parameter (isolate name, target, etc.).

    Raises :class:`ValueError` if *value* is empty or contains characters
    outside the ``[a-zA-Z0-9_.]`` set (must start with letter or ``_``).
    """
    if not value or not _VALID_IVY_PARAM.match(value):
        raise ValueError(f"Invalid Ivy parameter: {value!r}")
    return value
