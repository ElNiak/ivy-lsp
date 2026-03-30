"""Name utilities for Ivy qualified names."""

from __future__ import annotations


def get_last_component(qualified_name: str) -> str:
    """Extract the leaf from a dotted name: ``'a.b.c'`` -> ``'c'``, ``'c'`` -> ``'c'``."""
    return (
        qualified_name.rsplit(".", 1)[-1] if "." in qualified_name else qualified_name
    )
