"""Typed requirement graph with nodes, edges, and query methods.

The graph is built eagerly during workspace indexing and supports
queries for cross-file requirement propagation, state-variable impact
analysis, and orphan detection.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from ivy_lsp.infra.utils.name_utils import get_last_component

if TYPE_CHECKING:
    from ivy_lsp.core.semantic.nodes import RfcRequirement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot (thread-safe frozen copy for read-only use)
# ---------------------------------------------------------------------------


@dataclass
class GraphSnapshot:
    """Immutable snapshot of a RequirementGraph for thread-safe reads.

    Created via ``RequirementGraph.snapshot()`` under the graph lock.
    All query methods operate on the frozen data without needing locks.
    """

    actions: Dict[str, "ActionNode"]
    requirements: Dict[str, "RequirementNode"]
    state_vars: Dict[str, "StateVarNode"]
    properties: Dict[str, "PropertyNode"]
    rfc_requirements: Dict[str, "RfcRequirement"]
    edges: List[Tuple[str, "EdgeType", str]]
    outgoing: Dict[str, List[Tuple["EdgeType", str]]]
    incoming: Dict[str, List[Tuple["EdgeType", str]]]

    def _query_edges(
        self,
        node_id: str,
        direction: str,
        edge_type: "EdgeType",
        node_dict: dict,
    ) -> list:
        """Generic edge traversal returning nodes matching *edge_type*."""
        edges = (self.outgoing if direction == "outgoing" else self.incoming).get(
            node_id, []
        )
        return [
            node_dict[tid]
            for etype, tid in edges
            if etype == edge_type and tid in node_dict
        ]

    def get_requirements_for_action(self, action_name: str) -> List["RequirementNode"]:
        """Return requirements constraining the given action."""
        return self._query_edges(
            action_name, "incoming", EdgeType.CONSTRAINS, self.requirements
        )

    def get_state_vars_read_by(self, requirement_id: str) -> List["StateVarNode"]:
        """Return state variables read by a requirement."""
        return self._query_edges(
            requirement_id, "outgoing", EdgeType.READS, self.state_vars
        )

    def get_all_state_vars_written(self) -> List["StateVarNode"]:
        """Return all state variables with WRITES edges."""
        written: Set[str] = set()
        for _src, etype, dst in self.edges:
            if etype == EdgeType.WRITES:
                written.add(dst)
        return [self.state_vars[v] for v in written if v in self.state_vars]

    def get_uncovered_requirements(self) -> List["RfcRequirement"]:
        """Return RFC requirements not covered by any bracket tag."""
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        rfc_keys = set(self.rfc_requirements.keys())
        covered_ids: Set[str] = set()
        for req in self.requirements.values():
            for tag in req.bracket_tags:
                covered_ids.update(normalize_tag_to_manifest_ids(tag, rfc_keys))
        covered_ids &= rfc_keys
        return [
            r for r_id, r in self.rfc_requirements.items() if r_id not in covered_ids
        ]

    def get_coverage_stats(self) -> Dict[str, int]:
        """Compute covered/uncovered/total RFC requirement counts."""
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        rfc_keys = set(self.rfc_requirements.keys())
        covered_ids: Set[str] = set()
        for req in self.requirements.values():
            for tag in req.bracket_tags:
                covered_ids.update(normalize_tag_to_manifest_ids(tag, rfc_keys))
        covered_ids &= rfc_keys
        return {
            "total": len(self.rfc_requirements),
            "covered": len(covered_ids),
            "uncovered": len(self.rfc_requirements) - len(covered_ids),
        }

    def get_requirements_sharing_state_var(
        self, var_name: str
    ) -> List["RequirementNode"]:
        """Return requirements that read the given state variable."""
        return self._query_edges(
            var_name, "incoming", EdgeType.READS, self.requirements
        )

    def get_state_vars_written_by_action(self, action_id: str) -> List["StateVarNode"]:
        """Return state vars written within the same files as an action's monitors.

        Correlates WRITES edge source IDs (``filepath:line:write:var``) with
        the files containing requirements that CONSTRAIN this action.
        """
        # Collect files that have requirements for this action
        action_files: Set[str] = set()
        for etype, source_id in self.incoming.get(action_id, []):
            if etype == EdgeType.CONSTRAINS and source_id in self.requirements:
                action_files.add(self.requirements[source_id].file)

        if not action_files:
            return []

        # Find WRITES edges whose source ID starts with one of those files
        written_vars: Set[str] = set()
        for src, etype, dst in self.edges:
            if etype != EdgeType.WRITES:
                continue
            # Source format: "filepath:line:write:var_name"
            write_marker = ":write:"
            marker_idx = src.find(write_marker)
            if marker_idx < 0:
                continue
            # Everything before ":line:write:" is the filepath
            prefix = src[:marker_idx]
            # Remove the trailing ":line" to get the filepath
            last_colon = prefix.rfind(":")
            if last_colon > 0:
                filepath = prefix[:last_colon]
            else:
                filepath = prefix
            if filepath in action_files:
                written_vars.add(dst)

        return [self.state_vars[v] for v in written_vars if v in self.state_vars]


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
    mixin_kind: str  # "before" | "after" | "around" | "implement" | "direct"
    bracket_tags: List[str] = field(
        default_factory=list
    )  # parsed from "# [4]" or "# [rfc9000:4.1, rfc9000:8.1]"
    ast_node: Any = None  # reference to original AST node
    end_col: int = 0  # 0-based; 0 falls through to _DEFAULT_END_COLUMN at to_lsp()


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
    start_col: int = 0  # 0-based; 0 falls through to line-start
    end_col: int = 0  # 0-based; 0 falls through to _DEFAULT_END_COLUMN at to_lsp()


@dataclass
class ActionNode:
    """An action declaration in the workspace."""

    id: str  # qualified name
    name: str
    qualified_name: str
    file: str
    line: int
    start_col: int = 0  # 0-based; 0 falls through to line-start
    end_col: int = 0  # 0-based; 0 falls through to _DEFAULT_END_COLUMN at to_lsp()


@dataclass
class PropertyNode:
    """An invariant, property, axiom, or conjecture declaration."""

    id: str  # "filepath:line"
    kind: str  # "invariant" | "property" | "axiom" | "conjecture"
    name: str
    formula_text: str
    file: str
    line: int
    start_col: int = 0  # 0-based; 0 falls through to line-start
    end_col: int = 0  # 0-based; 0 falls through to _DEFAULT_END_COLUMN at to_lsp()


# ---------------------------------------------------------------------------
# Edge types
# ---------------------------------------------------------------------------


class EdgeType(Enum):
    """Edge types in the requirement dependency graph."""

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

    All mutations and queries are guarded by an ``RLock`` for thread safety
    (background deep indexer writes while LSP handlers read concurrently).
    """

    def __init__(self) -> None:
        """Initialize empty graph with lock and adjacency indices."""
        self._lock = threading.RLock()

        self.requirements: Dict[str, RequirementNode] = {}
        self.state_vars: Dict[str, StateVarNode] = {}
        self.actions: Dict[str, ActionNode] = {}
        self.properties: Dict[str, PropertyNode] = {}
        self.rfc_requirements: Dict[str, RfcRequirement] = {}

        self.edges: List[Tuple[str, EdgeType, str]] = []
        self._edge_set: Set[Tuple[str, EdgeType, str]] = set()
        self._outgoing: Dict[str, List[Tuple[EdgeType, str]]] = defaultdict(list)
        self._incoming: Dict[str, List[Tuple[EdgeType, str]]] = defaultdict(list)

        # Version-stamped cache for get_active_requirements_for_file.
        # Keyed by (filepath, version); invalidated on any graph mutation.
        self._version: int = 0
        self._active_reqs_cache: Dict[Tuple[str, int], List[RequirementNode]] = {}

        # Versioned snapshot cache – avoids rebuilding identical snapshots
        # when no mutations have occurred between consecutive reads.
        self._cached_snapshot: Optional[GraphSnapshot] = None
        self._cached_snapshot_version: int = -1

    # -- Pickle support (shared cache) ------------------------------------

    def __getstate__(self) -> dict:
        """Strip the unpicklable RLock before serialization."""
        state = self.__dict__.copy()
        del state["_lock"]
        return state

    def __setstate__(self, state: dict) -> None:
        """Rebuild the RLock after deserialization."""
        self.__dict__.update(state)
        self._lock = threading.RLock()

    # -- Mutation -----------------------------------------------------------

    def add_requirement(self, node: RequirementNode) -> None:
        """Add a requirement node to the graph."""
        with self._lock:
            self.requirements[node.id] = node
            self._version += 1

    def add_state_var(self, node: StateVarNode) -> None:
        """Add a state variable node to the graph."""
        with self._lock:
            self.state_vars[node.id] = node
            self._version += 1

    def add_action(self, node: ActionNode) -> None:
        """Add an action node to the graph."""
        with self._lock:
            self.actions[node.id] = node
            self._version += 1

    def add_action_if_absent(self, node: ActionNode) -> None:
        """Add action only if not already present (atomic check-and-write)."""
        with self._lock:
            if node.id not in self.actions:
                self.actions[node.id] = node
                self._version += 1

    def add_property(self, node: PropertyNode) -> None:
        """Add a property node to the graph."""
        with self._lock:
            self.properties[node.id] = node
            self._version += 1

    def add_rfc_requirement(self, node: RfcRequirement) -> None:
        """Add an RFC requirement node to the graph."""
        with self._lock:
            self.rfc_requirements[node.id] = node
            self._version += 1

    def add_edge(self, source_id: str, edge_type: EdgeType, target_id: str) -> None:
        """Add a typed edge between two nodes (deduplicated)."""
        with self._lock:
            edge = (source_id, edge_type, target_id)
            if edge in self._edge_set:
                return
            self._edge_set.add(edge)
            self.edges.append(edge)
            self._outgoing[source_id].append((edge_type, target_id))
            self._incoming[target_id].append((edge_type, source_id))
            self._version += 1

    def remove_file(self, target_filepath: str) -> None:
        """Remove all nodes and edges originating from *target_filepath*."""
        with self._lock:
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

            # Also collect synthetic write-edge source IDs for this file.
            # Write edges use IDs like "filepath:line:write:var" that aren't
            # stored as nodes, so they must be identified from the edge list.
            prefix = target_filepath + ":"
            for s, etype, _d in self.edges:
                if etype == EdgeType.WRITES and s.startswith(prefix):
                    removed_ids.add(s)

            if not removed_ids:
                return

            self.edges = [
                (s, t, d)
                for s, t, d in self.edges
                if s not in removed_ids and d not in removed_ids
            ]
            self._edge_set = set(self.edges)
            self._rebuild_adjacency()
            self._version += 1

    def add_file_requirements(
        self,
        filepath: str,
        reqs: List[RequirementNode],
        writes: Optional[List[Tuple[str, str, int]]] = None,
    ) -> None:
        """Bulk-add requirements from a single file and wire CONSTRAINS edges."""
        with self._lock:
            # Clear stale nodes/edges from previous index of this file.
            # remove_file() also acquires self._lock (RLock), safe re-entrantly.
            self.remove_file(filepath)

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

        Clears existing wiring edges first to prevent duplicates on re-call.
        Call after all files are indexed so ``known_vars`` is complete.
        """
        with self._lock:
            self.clear_wiring_edges()

            from ivy_lsp.core.analysis.formula_analyzer import (
                extract_state_var_references_text,
            )

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
        with self._lock:
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
        """Match bracket_tags on RequirementNodes to rfc_requirements keys, add COVERS edges.

        Uses tag normalization so bare tags like '4' match 'rfc9000:4.1' etc.
        """
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        with self._lock:
            existing = {(s, t) for s, et, t in self.edges if et == EdgeType.COVERS}
            rfc_keys = set(self.rfc_requirements.keys())
            for req in self.requirements.values():
                for tag in req.bracket_tags:
                    for rfc_id in normalize_tag_to_manifest_ids(tag, rfc_keys):
                        if (req.id, rfc_id) not in existing:
                            self.add_edge(req.id, EdgeType.COVERS, rfc_id)

    def clear_wiring_edges(self) -> None:
        """Remove all READS, DEPENDS_ON, and PROPAGATED_FROM edges.

        Called before re-wiring to prevent duplicate edges on repeated indexing.
        Preserves CONSTRAINS, WRITES, and COVERS edges.
        """
        with self._lock:
            keep_types = {EdgeType.CONSTRAINS, EdgeType.WRITES, EdgeType.COVERS}
            self.edges = [(s, t, d) for s, t, d in self.edges if t in keep_types]
            self._edge_set = set(self.edges)
            self._rebuild_adjacency()
            self._version += 1

    def populate_actions_from_symbols(self, symbols: List[Any]) -> None:
        """Create ActionNodes from symbol table entries.

        Also creates nodes from monitor_action references in existing
        requirements to ensure every CONSTRAINS target has a corresponding
        ActionNode.
        """
        from lsprotocol.types import SymbolKind

        with self._lock:
            for sym in symbols:
                if sym.kind in (SymbolKind.Function, SymbolKind.Method):
                    if sym.name not in self.actions:
                        # IvySymbol.range is (start_line, start_col, end_line, end_col).
                        sym_range = sym.range if sym.range else None
                        self.add_action(
                            ActionNode(
                                id=sym.name,
                                name=get_last_component(sym.name),
                                qualified_name=sym.name,
                                file=sym.file_path or "",
                                line=sym_range[0] if sym_range else 0,
                                start_col=sym_range[1] if sym_range else 0,
                                end_col=sym_range[3] if sym_range else 0,
                            )
                        )

            for req in self.requirements.values():
                if req.monitor_action and req.monitor_action not in self.actions:
                    self.add_action(
                        ActionNode(
                            id=req.monitor_action,
                            name=get_last_component(req.monitor_action),
                            qualified_name=req.monitor_action,
                            file=req.file,
                            line=req.line,
                            # No symbol available for monitor_action references;
                            # leave start_col/end_col at default 0.
                        )
                    )

    def populate_state_vars(self, known_vars: Set[str], symbols: List[Any]) -> None:
        """Create StateVarNodes from known variable names and the symbol table."""
        from lsprotocol.types import SymbolKind

        with self._lock:
            sym_lookup: Dict[str, Any] = {}
            for sym in symbols:
                if sym.kind in (SymbolKind.Variable, SymbolKind.Field):
                    sym_lookup[sym.name] = sym

            for var_name in known_vars:
                if var_name not in self.state_vars:
                    sym = sym_lookup.get(var_name)
                    sym_range = sym.range if sym and sym.range else None
                    self.add_state_var(
                        StateVarNode(
                            id=var_name,
                            name=get_last_component(var_name),
                            qualified_name=var_name,
                            file=sym.file_path if sym and sym.file_path else "",
                            line=sym_range[0] if sym_range else 0,
                            is_relation=False,
                            start_col=sym_range[1] if sym_range else 0,
                            end_col=sym_range[3] if sym_range else 0,
                        )
                    )

    def wire_propagation_edges(self, include_graph: Any) -> None:
        """Wire PROPAGATED_FROM edges for cross-file requirement propagation.

        For each requirement, finds files that include the requirement's
        source file and creates edges representing the propagation chain.
        """
        with self._lock:
            for req in self.requirements.values():
                included_by = include_graph.get_included_by(req.file)
                for target_file in included_by:
                    self.add_edge(req.id, EdgeType.PROPAGATED_FROM, target_file)

    def get_coverage_stats(self) -> Dict[str, int]:
        """Compute covered/uncovered/total by level."""
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        with self._lock:
            rfc_keys = set(self.rfc_requirements.keys())
            covered_ids: Set[str] = set()
            for req in self.requirements.values():
                for tag in req.bracket_tags:
                    covered_ids.update(normalize_tag_to_manifest_ids(tag, rfc_keys))
            covered_ids &= rfc_keys
            return {
                "total": len(self.rfc_requirements),
                "covered": len(covered_ids),
                "uncovered": len(self.rfc_requirements) - len(covered_ids),
            }

    def get_uncovered_requirements(self) -> List[RfcRequirement]:
        """Return RfcRequirement nodes with no COVERS edge."""
        from ivy_lsp.core.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        with self._lock:
            rfc_keys = set(self.rfc_requirements.keys())
            covered_ids: Set[str] = set()
            for req in self.requirements.values():
                for tag in req.bracket_tags:
                    covered_ids.update(normalize_tag_to_manifest_ids(tag, rfc_keys))
            covered_ids &= rfc_keys
            return [
                r
                for r_id, r in self.rfc_requirements.items()
                if r_id not in covered_ids
            ]

    def get_outgoing_edges(self, node_id: str) -> List[Tuple[EdgeType, str]]:
        """Return outgoing edges for the given node (copy of internal list)."""
        with self._lock:
            return list(self._outgoing.get(node_id, []))

    @property
    def outgoing(self) -> Dict[str, List[Tuple[EdgeType, str]]]:
        """Public accessor for the outgoing adjacency index."""
        return self._outgoing

    @property
    def incoming(self) -> Dict[str, List[Tuple[EdgeType, str]]]:
        """Public accessor for the incoming adjacency index."""
        return self._incoming

    def _rebuild_adjacency(self) -> None:
        """Rebuild adjacency indices from the edge list.

        IMPORTANT: Caller MUST hold ``self._lock`` before invoking.
        This method is not thread-safe on its own.
        """
        self._outgoing = defaultdict(list)
        self._incoming = defaultdict(list)
        for s, t, d in self.edges:
            self._outgoing[s].append((t, d))
            self._incoming[d].append((t, s))

    # -- Snapshot -----------------------------------------------------------

    def snapshot(self) -> GraphSnapshot:
        """Return a thread-safe frozen copy of all graph data for read-only use.

        Cached by graph version; repeated calls without mutations return
        the same GraphSnapshot instance.
        """
        with self._lock:
            if (
                self._cached_snapshot is not None
                and self._cached_snapshot_version == self._version
            ):
                return self._cached_snapshot
            snap = GraphSnapshot(
                actions=dict(self.actions),
                requirements=dict(self.requirements),
                state_vars=dict(self.state_vars),
                properties=dict(self.properties),
                rfc_requirements=dict(self.rfc_requirements),
                edges=list(self.edges),
                outgoing={k: list(v) for k, v in self._outgoing.items()},
                incoming={k: list(v) for k, v in self._incoming.items()},
            )
            self._cached_snapshot = snap
            self._cached_snapshot_version = self._version
            return snap

    # -- Queries ------------------------------------------------------------

    def _query_edges(
        self,
        node_id: str,
        direction: str,
        edge_type: EdgeType,
        node_dict: dict,
    ) -> list:
        """Generic locked edge traversal returning nodes matching *edge_type*."""
        with self._lock:
            edges = (self._outgoing if direction == "outgoing" else self._incoming).get(
                node_id, []
            )
            return [
                node_dict[tid]
                for etype, tid in edges
                if etype == edge_type and tid in node_dict
            ]

    def get_requirements_for_action(self, action_name: str) -> List[RequirementNode]:
        """Return all requirements constraining *action_name*."""
        return self._query_edges(
            action_name, "incoming", EdgeType.CONSTRAINS, self.requirements
        )

    def get_state_vars_read_by(self, requirement_id: str) -> List[StateVarNode]:
        """Return state variables read by a requirement."""
        return self._query_edges(
            requirement_id, "outgoing", EdgeType.READS, self.state_vars
        )

    def get_all_state_vars_written(self) -> List[StateVarNode]:
        """Return all state vars with WRITES edges in the graph.

        Returns a conservative superset (all WRITES targets across the
        entire graph). A future refinement may track writes per-action
        for finer granularity.
        """
        with self._lock:
            written: Set[str] = set()
            for _src, etype, dst in self.edges:
                if etype == EdgeType.WRITES:
                    written.add(dst)
            return [self.state_vars[v] for v in written if v in self.state_vars]

    def get_requirements_sharing_state_var(
        self, var_name: str
    ) -> List[RequirementNode]:
        """Return requirements that read *var_name*."""
        return self._query_edges(
            var_name, "incoming", EdgeType.READS, self.requirements
        )

    def get_properties_depending_on_axiom(self, axiom_id: str) -> List[PropertyNode]:
        """Return properties that depend on *axiom_id*."""
        return self._query_edges(
            axiom_id, "incoming", EdgeType.DEPENDS_ON, self.properties
        )

    def get_active_requirements_for_file(
        self, filepath: str, include_graph: Any
    ) -> List[RequirementNode]:
        """Return requirements active in *filepath* (own + transitive includes).

        Results are cached by ``(filepath, self._version)``; any graph
        mutation bumps the version and implicitly invalidates all entries.
        """
        with self._lock:
            cache_key = (filepath, self._version)
            cached = self._active_reqs_cache.get(cache_key)
            if cached is not None:
                return cached

            active_files = {filepath}
            active_files |= include_graph.get_transitive_includes(filepath)
            result = [r for r in self.requirements.values() if r.file in active_files]

            # Keep cache bounded to avoid unbounded memory growth.
            if len(self._active_reqs_cache) > 200:
                self._active_reqs_cache.clear()
            self._active_reqs_cache[cache_key] = result
            return result

    def get_impact_of_state_var_change(
        self, var_name: str
    ) -> Dict[str, List[RequirementNode]]:
        """Return a dict mapping file paths to requirements reading *var_name*."""
        with self._lock:
            by_file: Dict[str, List[RequirementNode]] = defaultdict(list)
            for req in self.get_requirements_sharing_state_var(var_name):
                by_file[req.file].append(req)
            return dict(by_file)

    def get_requirement_counts_for_action(self, action_name: str) -> Dict[str, int]:
        """Return counts by kind for requirements constraining *action_name*."""
        with self._lock:
            counts: Dict[str, int] = defaultdict(int)
            for req in self.get_requirements_for_action(action_name):
                counts[req.kind] += 1
            return dict(counts)

    def get_all_requirements_in_file(self, filepath: str) -> List[RequirementNode]:
        """Return all requirements defined in *filepath*."""
        with self._lock:
            return [r for r in self.requirements.values() if r.file == filepath]

    def get_all_state_var_names(self) -> Set[str]:
        """Return all known state variable names."""
        with self._lock:
            return set(self.state_vars.keys())
