"""Semantic model node types.

Extends the existing RequirementNode / PropertyNode concept with richer
node types that capture type information, RFC traceability, and tier
provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

__all__ = [
    "SymbolNode",
    "TypeNode",
    "MonitorNode",
    "RfcRequirement",
    "RfcAnnotation",
    "ManifestMetadata",
]

Tier = Literal["tier1", "tier2", "tier3"]


@dataclass
class SymbolNode:
    """A symbol (action, relation, function, individual) in the workspace.

    Subsumes StateVarNode + ActionNode with richer type metadata.
    """

    id: str
    name: str
    qualified_name: str
    kind: Literal[
        "action",
        "relation",
        "function",
        "individual",
        "module",
        "object",
        "isolate",
        "destructor",
        "constructor",
        "instance",
    ]
    file: str
    line: int
    col: int = 0
    sort_name: Optional[str] = None
    arity: int = 0
    params: List[str] = field(default_factory=list)
    return_sort: Optional[str] = None
    tier: Tier = "tier1"


@dataclass
class TypeNode:
    """A type declaration in the workspace."""

    id: str
    name: str
    qualified_name: str
    file: str
    line: int
    sort_name: Optional[str] = None
    is_enum: bool = False
    variants: List[str] = field(default_factory=list)
    tier: Tier = "tier1"


@dataclass
class MonitorNode:
    """A before/after/around monitor block."""

    id: str
    action_name: str
    mixin_kind: Literal["before", "after", "around"]
    file: str
    line: int
    requirement_ids: List[str] = field(default_factory=list)


@dataclass
class RfcRequirement:
    """A single requirement extracted from an RFC manifest."""

    id: str  # tag key e.g. "rfc9000:4.1"
    rfc: str  # e.g. "RFC9000"
    section: str  # e.g. "4.1"
    text: str
    level: Literal["MUST", "SHOULD", "MAY", "MUST NOT", "SHOULD NOT"]
    layer: str = ""  # e.g. "frame", "connection", "transport"
    testable: bool = True


@dataclass
class ManifestMetadata:
    """Metadata for a requirement manifest, enabling staleness detection."""

    generated_at: str = ""  # ISO 8601 timestamp
    generator_version: str = ""
    source: str = ""  # URL or file path of the source RFC
    content_hash: str = ""  # SHA-256 of the source text
    last_checked: str = ""  # ISO 8601 timestamp of last staleness check
    obsoleted_by: str = ""  # RFC number that obsoletes this one
    updated_by: str = ""  # Comma-separated RFC numbers
    errata_ids: str = ""  # Comma-separated errata IDs
    is_draft: bool = False
    draft_name: str = ""
    draft_version: str = ""


@dataclass
class RfcAnnotation:
    """An RFC bracket-tag annotation found in source code."""

    id: str  # "filepath:line:0" (unique per source line)
    file: str
    line: int
    tags: List[str] = field(default_factory=list)  # e.g. ["rfc9000:4.1", "rfc9000:8.1"]
    node_id: Optional[str] = None  # linked semantic node id
