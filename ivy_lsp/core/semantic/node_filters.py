"""Type-based node filtering helpers for SemanticModel queries."""

from __future__ import annotations

from typing import Iterable, TypeVar

T = TypeVar("T")


def nodes_of_type(nodes: Iterable, node_type: type[T]) -> list[T]:
    """Filter nodes by type."""
    return [n for n in nodes if isinstance(n, node_type)]


def first_node_of_type(nodes: Iterable, node_type: type[T]) -> T | None:
    """First node of type, or None."""
    return next((n for n in nodes if isinstance(n, node_type)), None)
