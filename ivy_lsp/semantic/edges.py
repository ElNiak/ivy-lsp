"""Semantic model edge types.

Extends the existing EdgeType enum with richer relationship types for
the unified semantic model.
"""

from __future__ import annotations

from enum import Enum

# Re-export existing EdgeType for backward compatibility
from ivy_lsp.analysis.requirement_graph import EdgeType

__all__ = ["EdgeType", "SemanticEdgeType"]


class SemanticEdgeType(Enum):
    """Extended edge types for the unified semantic model."""

    # Existing (mirrored from EdgeType for unified use)
    READS = "reads"
    WRITES = "writes"
    CONSTRAINS = "constrains"
    DEPENDS_ON = "depends_on"
    PROPAGATED_FROM = "propagated_from"

    # New semantic relationships
    HAS_TYPE = "has_type"
    HAS_PARAM = "has_param"
    RETURNS_TYPE = "returns_type"
    MONITORS = "monitors"
    EXPORTS = "exports"
    IMPORTS = "imports"
    CONTAINS = "contains"
    INCLUDES = "includes"
    COVERS = "covers"  # RFC coverage: assertion -> RfcRequirement
