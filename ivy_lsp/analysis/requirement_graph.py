"""Typed requirement graph with nodes, edges, and query methods.

The graph is built eagerly during workspace indexing and supports
queries for cross-file requirement propagation, state-variable impact
analysis, and orphan detection.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from ivy_lsp.semantic.nodes import RfcRequirement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------


@dataclass
class RequirementNode:
    """A require/ensure/assume/assert statement extracted from a monitor body."""

    id: str  # "filepath:line" unique key
    kind: str  # "require" | "ensure" | "assume" | "assert"
    formula_text: str
    line: int  # 0-based
    col: int  # 0-based
    file: str  # absolute filepath
    monitor_action: str  # action being monitored (mixee)
    mixin_kind: str  # "before" | "after" | "implement" | "direct"
    bracket_tags: List[str] = field(default_factory=list)  # parsed from "# [4]" or "# [rfc9000:4.1, rfc9000:8.1]"
    ast_node: Any = None  # reference to original AST node


@dataclass
class StateVarNode:
    """A state variable (relation, function, individual) in the workspace."""

    id: str  # qualified name
    name: str  # e.g. "sent_pkt"
    qualified_name: str  # e.g. "frame.ack.sent_pkt"
    file: str
    line: int
    is_relation: bool = False  # True if relation (bool sort + args)
    params: Optional[str] = None  # e.g. "(C:cid, L:quic_packet_type)"


@dataclass
class ActionNode:
    """An action declaration in the workspace."""

    id: str  # qualified name
    name: str
    qualified_name: str
    file: str
    line: int


@dataclass
class PropertyNode:
    """An invariant, property, axiom, or conjecture declaration."""

    id: str  # "filepath:line"
    kind: str  # "invariant" | "property" | "axiom" | "conjecture"
    name: str
    formula_text: str
    file: str
    line: int


# ---------------------------------------------------------------------------
# Edge types
# ---------------------------------------------------------------------------


class EdgeType(Enum):
    READS = "reads"  # requirement/property -> state var
    WRITES = "writes"  # assignment in body -> state var
    CONSTRAINS = "constrains"  # requirement -> action (before/after)
    DEPENDS_ON = "depends_on"  # invariant -> axiom (shared state vars)
    PROPAGATED_FROM = "propagated_from"  # cross-file via include chain
    COVERS = "covers"  # requirement node -> RFC requirement (bracket tag match)


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


class RequirementGraph:
    """Graph of requirements, state variables, actions, and properties.

    Edges are stored as ``(source_id, EdgeType, target_id)`` triples with
    adjacency indices for fast forward/backward traversal.
    """

    def __init__(self) -> None:
        self.requirements: Dict[str, RequirementNode] = {}
        self.state_vars: Dict[str, StateVarNode] = {}
        self.actions: Dict[str, ActionNode] = {}
        self.properties: Dict[str, PropertyNode] = {}
        self.rfc_requirements: Dict[str, RfcRequirement] = {}

        self.edges: List[Tuple[str, EdgeType, str]] = []
        self._outgoing: Dict[str, List[Tuple[EdgeType, str]]] = defaultdict(list)
        self._incoming: Dict[str, List[Tuple[EdgeType, str]]] = defaultdict(list)

    # -- Mutation -----------------------------------------------------------

    def add_requirement(self, node: RequirementNode) -> None:
        self.requirements[node.id] = node

    def add_state_var(self, node: StateVarNode) -> None:
        self.state_vars[node.id] = node

    def add_action(self, node: ActionNode) -> None:
        self.actions[node.id] = node

    def add_property(self, node: PropertyNode) -> None:
        self.properties[node.id] = node

    def add_rfc_requirement(self, node: RfcRequirement) -> None:
        self.rfc_requirements[node.id] = node

    def add_edge(self, source_id: str, edge_type: EdgeType, target_id: str) -> None:
        self.edges.append((source_id, edge_type, target_id))
        self._outgoing[source_id].append((edge_type, target_id))
        self._incoming[target_id].append((edge_type, source_id))

    def remove_file(self, target_filepath: str) -> None:
        """Remove all nodes and edges originating from *target_filepath*."""
        removed_ids: Set[str] = set()
        for rid, r in list(self.requirements.items()):
            if r.file == target_filepath:
                removed_ids.add(rid)
                del self.requirements[rid]
        for pid, p in list(self.properties.items()):
            if p.file == target_filepath:
                removed_ids.add(pid)
                del self.properties[pid]
        for aid, a in list(self.actions.items()):
            if a.file == target_filepath:
                removed_ids.add(aid)
                del self.actions[aid]
        for vid, v in list(self.state_vars.items()):
            if v.file == target_filepath:
                removed_ids.add(vid)
                del self.state_vars[vid]

        if not removed_ids:
            return

        self.edges = [
            (s, t, d)
            for s, t, d in self.edges
            if s not in removed_ids and d not in removed_ids
        ]
        self._rebuild_adjacency()

    def add_file_requirements(
        self,
        filepath: str,
        reqs: List[RequirementNode],
        writes: Optional[List[Tuple[str, str, int]]] = None,
    ) -> None:
        """Bulk-add requirements from a single file and wire CONSTRAINS edges."""
        for req in reqs:
            self.add_requirement(req)
            if req.monitor_action:
                self.add_edge(req.id, EdgeType.CONSTRAINS, req.monitor_action)

        if writes:
            for var_name, filepath_w, line in writes:
                write_id = f"{filepath_w}:{line}:write:{var_name}"
                self.add_edge(write_id, EdgeType.WRITES, var_name)

    def wire_state_var_edges(self, known_vars: Set[str]) -> None:
        """Wire READS edges from requirements/properties to state vars.

        Call after all files are indexed so ``known_vars`` is complete.
        """
        from ivy_lsp.analysis.formula_analyzer import extract_state_var_references_text

        for req in self.requirements.values():
            refs = extract_state_var_references_text(req.formula_text, known_vars)
            for var_name in refs:
                self.add_edge(req.id, EdgeType.READS, var_name)

        for prop in self.properties.values():
            refs = extract_state_var_references_text(prop.formula_text, known_vars)
            for var_name in refs:
                self.add_edge(prop.id, EdgeType.READS, var_name)

    def wire_dependency_edges(self) -> None:
        """Wire DEPENDS_ON edges between properties sharing state variables."""
        prop_to_vars: Dict[str, Set[str]] = {}
        for prop_id in self.properties:
            var_ids = {
                target
                for etype, target in self._outgoing.get(prop_id, [])
                if etype == EdgeType.READS
            }
            prop_to_vars[prop_id] = var_ids

        prop_ids = list(prop_to_vars.keys())
        for i, pid_a in enumerate(prop_ids):
            for pid_b in prop_ids[i + 1 :]:
                shared = prop_to_vars[pid_a] & prop_to_vars[pid_b]
                if shared:
                    pa = self.properties.get(pid_a)
                    pb = self.properties.get(pid_b)
                    if pa and pb:
                        if pa.kind == "axiom" and pb.kind != "axiom":
                            self.add_edge(pid_b, EdgeType.DEPENDS_ON, pid_a)
                        elif pb.kind == "axiom" and pa.kind != "axiom":
                            self.add_edge(pid_a, EdgeType.DEPENDS_ON, pid_b)

    def wire_coverage_edges(self) -> None:
        """Match bracket_tags on RequirementNodes to rfc_requirements keys, add COVERS edges."""
        existing = {(s, t) for s, et, t in self.edges if et == EdgeType.COVERS}
        for req in self.requirements.values():
            for tag in req.bracket_tags:
                if tag in self.rfc_requirements and (req.id, tag) not in existing:
                    self.add_edge(req.id, EdgeType.COVERS, tag)

    def get_coverage_stats(self) -> Dict[str, int]:
        """Compute covered/uncovered/total by level."""
        covered_ids: Set[str] = set()
        for req in self.requirements.values():
            for tag in req.bracket_tags:
                if tag in self.rfc_requirements:
                    covered_ids.add(tag)
        return {
            "total": len(self.rfc_requirements),
            "covered": len(covered_ids),
            "uncovered": len(self.rfc_requirements) - len(covered_ids),
        }

    def get_uncovered_requirements(self) -> List[RfcRequirement]:
        """Return RfcRequirement nodes with no COVERS edge."""
        covered_ids: Set[str] = set()
        for req in self.requirements.values():
            for tag in req.bracket_tags:
                if tag in self.rfc_requirements:
                    covered_ids.add(tag)
        return [r for r_id, r in self.rfc_requirements.items() if r_id not in covered_ids]

    def _rebuild_adjacency(self) -> None:
        self._outgoing = defaultdict(list)
        self._incoming = defaultdict(list)
        for s, t, d in self.edges:
            self._outgoing[s].append((t, d))
            self._incoming[d].append((t, s))

    # -- Queries ------------------------------------------------------------

    def get_requirements_for_action(self, action_name: str) -> List[RequirementNode]:
        """Return all requirements constraining *action_name*."""
        result = []
        for etype, source_id in self._incoming.get(action_name, []):
            if etype == EdgeType.CONSTRAINS and source_id in self.requirements:
                result.append(self.requirements[source_id])
        return result

    def get_state_vars_read_by(self, requirement_id: str) -> List[StateVarNode]:
        """Return state variables read by a requirement."""
        result = []
        for etype, target_id in self._outgoing.get(requirement_id, []):
            if etype == EdgeType.READS and target_id in self.state_vars:
                result.append(self.state_vars[target_id])
        return result

    def get_state_vars_written_in_monitor(
        self, action_name: str
    ) -> List[StateVarNode]:
        """Return state vars written in monitors of *action_name*."""
        written: Set[str] = set()
        for etype, source_id in self._incoming.get(action_name, []):
            if etype == EdgeType.CONSTRAINS:
                for etype2, target_id in self._outgoing.get(source_id, []):
                    if etype2 == EdgeType.WRITES:
                        written.add(target_id)
        return [self.state_vars[v] for v in written if v in self.state_vars]

    def get_requirements_sharing_state_var(
        self, var_name: str
    ) -> List[RequirementNode]:
        """Return requirements that read *var_name*."""
        result = []
        for etype, source_id in self._incoming.get(var_name, []):
            if etype == EdgeType.READS and source_id in self.requirements:
                result.append(self.requirements[source_id])
        return result

    def get_properties_depending_on_axiom(
        self, axiom_id: str
    ) -> List[PropertyNode]:
        """Return properties that depend on *axiom_id*."""
        result = []
        for etype, source_id in self._incoming.get(axiom_id, []):
            if etype == EdgeType.DEPENDS_ON and source_id in self.properties:
                result.append(self.properties[source_id])
        return result

    def get_active_requirements_for_file(
        self, filepath: str, include_graph: Any
    ) -> List[RequirementNode]:
        """Return requirements active in *filepath* (own + transitive includes)."""
        active_files = {filepath}
        active_files |= include_graph.get_transitive_includes(filepath)
        return [
            r for r in self.requirements.values() if r.file in active_files
        ]

    def get_impact_of_state_var_change(
        self, var_name: str
    ) -> Dict[str, List[RequirementNode]]:
        """Return a dict mapping file paths to requirements reading *var_name*."""
        by_file: Dict[str, List[RequirementNode]] = defaultdict(list)
        for req in self.get_requirements_sharing_state_var(var_name):
            by_file[req.file].append(req)
        return dict(by_file)

    def get_requirement_counts_for_action(
        self, action_name: str
    ) -> Dict[str, int]:
        """Return counts by kind for requirements constraining *action_name*."""
        counts: Dict[str, int] = defaultdict(int)
        for req in self.get_requirements_for_action(action_name):
            counts[req.kind] += 1
        return dict(counts)

    def get_all_requirements_in_file(self, filepath: str) -> List[RequirementNode]:
        """Return all requirements defined in *filepath*."""
        return [r for r in self.requirements.values() if r.file == filepath]

    def get_all_state_var_names(self) -> Set[str]:
        """Return all known state variable names."""
        return set(self.state_vars.keys())
