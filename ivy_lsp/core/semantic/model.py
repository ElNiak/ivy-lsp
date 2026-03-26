"""Thread-safe unified semantic model for the Ivy LSP analysis pipeline.

The SemanticModel stores all node and edge data produced by the three
analysis tiers.  LSP features and MCP tools query this single model
instead of maintaining their own data structures.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple, Type

from ivy_lsp.core.semantic.edges import SemanticEdgeType

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class SemanticModel:
    """Thread-safe graph of semantic nodes and edges.

    Nodes are arbitrary dataclass instances keyed by their ``id`` attribute.
    Edges are ``(source_id, SemanticEdgeType, target_id)`` triples.

    All mutations are guarded by a reentrant lock so that background Tier 3
    analysis can update the model while foreground features read it.
    """

    def __init__(self) -> None:
        """Initialize empty node/edge stores with a reentrant lock."""
        self._lock = threading.RLock()

        # Primary storage
        self._nodes: Dict[str, Any] = {}
        self._nodes_by_type: Dict[type, Dict[str, Any]] = defaultdict(dict)
        self._nodes_by_file: Dict[str, Set[str]] = defaultdict(set)
        self._node_tiers: Dict[str, str] = {}  # node_id -> tier
        self._nodes_by_name: Dict[str, List[Any]] = defaultdict(list)
        self._version: int = 0

        # Edges – use a set to prevent duplicate accumulation
        self._edges: Set[Tuple[str, SemanticEdgeType, str]] = set()
        self._outgoing: Dict[str, List[Tuple[SemanticEdgeType, str]]] = defaultdict(
            list
        )
        self._incoming: Dict[str, List[Tuple[SemanticEdgeType, str]]] = defaultdict(
            list
        )

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

    # -- Name index helpers (caller must hold _lock) -------------------------

    def _unindex_name(self, name: str, node_id: str) -> None:
        name_list = self._nodes_by_name.get(name)
        if name_list:
            self._nodes_by_name[name] = [n for n in name_list if n.id != node_id]
            if not self._nodes_by_name[name]:
                del self._nodes_by_name[name]

    def _index_name(self, name: str, node_id: str, node: Any) -> None:
        existing = self._nodes_by_name.get(name, [])
        self._nodes_by_name[name] = [n for n in existing if n.id != node_id] + [node]

    # -- Mutation -----------------------------------------------------------

    def add_node(self, node: Any) -> None:
        """Add or replace a node in the model."""
        node_id = node.id
        name = getattr(node, "name", None)
        with self._lock:
            old_node = self._nodes.get(node_id)
            if old_node is not None:
                old_name = getattr(old_node, "name", None)
                if old_name and old_name != name:
                    self._unindex_name(old_name, node_id)
            self._nodes[node_id] = node
            self._nodes_by_type[type(node)][node_id] = node
            if name:
                self._index_name(name, node_id, node)
            file_attr = getattr(node, "file", None)
            if file_attr:
                self._nodes_by_file[file_attr].add(node_id)
            tier = getattr(node, "tier", None)
            if tier:
                self._node_tiers[node_id] = tier
            self._version += 1

    def add_edge(
        self, source_id: str, edge_type: SemanticEdgeType, target_id: str
    ) -> None:
        """Add a directed edge (idempotent)."""
        with self._lock:
            edge = (source_id, edge_type, target_id)
            if edge in self._edges:
                return
            self._edges.add(edge)
            self._outgoing[source_id].append((edge_type, target_id))
            self._incoming[target_id].append((edge_type, source_id))

    def remove_file(self, filepath: str) -> None:
        """Remove all nodes and edges originating from *filepath*."""
        with self._lock:
            node_ids = set(self._nodes_by_file.pop(filepath, set()))
            if not node_ids:
                return
            for nid in node_ids:
                node = self._nodes.pop(nid, None)
                if node is not None:
                    type_dict = self._nodes_by_type.get(type(node))
                    if type_dict:
                        type_dict.pop(nid, None)
                    name = getattr(node, "name", None)
                    if name:
                        self._unindex_name(name, nid)
                self._node_tiers.pop(nid, None)
            edges_to_remove = {
                (src, etype, dst)
                for src, etype, dst in self._edges
                if src in node_ids or dst in node_ids
            }
            self._edges -= edges_to_remove
            for src, etype, dst in edges_to_remove:
                adj_list = self._outgoing.get(src)
                if adj_list is not None:
                    try:
                        adj_list.remove((etype, dst))
                    except ValueError:
                        pass
                adj_list = self._incoming.get(dst)
                if adj_list is not None:
                    try:
                        adj_list.remove((etype, src))
                    except ValueError:
                        pass
            self._version += 1

    def update_file(
        self,
        filepath: str,
        nodes: List[Any],
        edges: List[Tuple[str, SemanticEdgeType, str]],
        tier: str,
    ) -> None:
        """Atomically replace nodes/edges for *filepath* at *tier*.

        Preserves data from other tiers for the same file.  Higher tiers
        overwrite lower-tier data for the same node id.
        """
        tier_rank = {"tier1": 1, "tier2": 2, "tier3": 3}
        new_rank = tier_rank.get(tier, 0)

        with self._lock:
            # Collect ids to remove: same file, same or lower tier
            old_ids = set(self._nodes_by_file.get(filepath, set()))
            ids_to_remove: Set[str] = set()
            for nid in old_ids:
                existing_tier = self._node_tiers.get(nid, "tier1")
                existing_rank = tier_rank.get(existing_tier, 0)
                if existing_rank <= new_rank:
                    ids_to_remove.add(nid)

            # Remove old nodes
            for nid in ids_to_remove:
                node = self._nodes.pop(nid, None)
                if node is not None:
                    type_dict = self._nodes_by_type.get(type(node))
                    if type_dict:
                        type_dict.pop(nid, None)
                    name = getattr(node, "name", None)
                    if name:
                        self._unindex_name(name, nid)
                self._node_tiers.pop(nid, None)
                file_set = self._nodes_by_file.get(filepath)
                if file_set:
                    file_set.discard(nid)

            # Remove old edges involving removed ids (incremental)
            if ids_to_remove:
                edges_to_remove = {
                    (src, etype, dst)
                    for src, etype, dst in self._edges
                    if src in ids_to_remove or dst in ids_to_remove
                }
                self._edges -= edges_to_remove
                for src, etype, dst in edges_to_remove:
                    adj_list = self._outgoing.get(src)
                    if adj_list is not None:
                        try:
                            adj_list.remove((etype, dst))
                        except ValueError:
                            pass
                    adj_list = self._incoming.get(dst)
                    if adj_list is not None:
                        try:
                            adj_list.remove((etype, src))
                        except ValueError:
                            pass

            # Add new nodes (skip if existing node at higher tier)
            for node in nodes:
                nid = node.id
                existing_tier = self._node_tiers.get(nid)
                if existing_tier and tier_rank.get(existing_tier, 0) > new_rank:
                    continue  # preserve higher-tier node
                # Clean up old type index entry if the node already exists
                # (may be a different type at the old tier)
                name = getattr(node, "name", None)
                old_node = self._nodes.get(nid)
                if old_node is not None:
                    old_type_dict = self._nodes_by_type.get(type(old_node))
                    if old_type_dict:
                        old_type_dict.pop(nid, None)
                    old_name = getattr(old_node, "name", None)
                    if old_name and old_name != name:
                        self._unindex_name(old_name, nid)
                self._nodes[nid] = node
                self._nodes_by_type[type(node)][nid] = node
                self._nodes_by_file[filepath].add(nid)
                self._node_tiers[nid] = tier
                if name:
                    self._index_name(name, nid, node)

            # Add new edges (incremental, skip duplicates)
            for src, etype, dst in edges:
                edge = (src, etype, dst)
                if edge not in self._edges:
                    self._edges.add(edge)
                    self._outgoing[src].append((etype, dst))
                    self._incoming[dst].append((etype, src))
            self._version += 1

    def _rebuild_adjacency(self) -> None:
        """Rebuild adjacency indices from the edge list.

        IMPORTANT: Caller MUST hold ``self._lock`` before invoking.
        This method is not thread-safe on its own.
        """
        self._outgoing = defaultdict(list)
        self._incoming = defaultdict(list)
        for src, etype, dst in self._edges:
            self._outgoing[src].append((etype, dst))
            self._incoming[dst].append((etype, src))

    def merge_from(self, other: "SemanticModel") -> None:
        """Merge all nodes and edges from *other* into this model.

        Used to combine per-protocol SemanticModels (from offline indexes)
        into a single workspace-wide model for the MCP server.

        Both ``add_node`` and ``add_edge`` are idempotent, so merging
        the same model twice is safe.  If two protocols produce nodes
        with the same ``id``, the last merge wins (consistent with
        ``add_node`` replace semantics).

        Note: ``other._lock`` is NOT acquired — the caller must ensure
        ``other`` is not being concurrently mutated (e.g. a deserialized
        offline model with no active writers).
        """
        with self._lock:
            # Inline node/edge insertion (avoids per-item lock re-acquisition)
            for node in other._nodes.values():
                node_id = node.id
                self._nodes[node_id] = node
                self._nodes_by_type[type(node)][node_id] = node
                file_attr = getattr(node, "file", None)
                if file_attr:
                    self._nodes_by_file[file_attr].add(node_id)
                tier = getattr(node, "tier", None)
                if tier:
                    self._node_tiers[node_id] = tier
                name = getattr(node, "name", None)
                if name:
                    self._index_name(name, node_id, node)
            # Copy edges
            for src, etype, dst in other._edges:
                edge = (src, etype, dst)
                if edge not in self._edges:
                    self._edges.add(edge)
                    self._outgoing[src].append((etype, dst))
                    self._incoming[dst].append((etype, src))
            self._version += 1

    # -- Queries ------------------------------------------------------------

    def get_node(self, node_id: str) -> Optional[Any]:
        """Return a node by id, or None."""
        with self._lock:
            return self._nodes.get(node_id)

    @property
    def version(self) -> int:
        """Monotonic version counter; incremented on every mutation."""
        with self._lock:
            return self._version

    def get_nodes_by_type(self, node_type: Type) -> List[Any]:
        """Return all nodes of a given type."""
        with self._lock:
            return list(self._nodes_by_type.get(node_type, {}).values())

    def get_nodes_by_name(self, name: str) -> List[Any]:
        """Return all nodes with the given name (O(1) lookup)."""
        with self._lock:
            return list(self._nodes_by_name.get(name, []))

    def get_nodes_in_file(self, filepath: str) -> List[Any]:
        """Return all nodes defined in *filepath*."""
        with self._lock:
            ids = self._nodes_by_file.get(filepath, set())
            return [self._nodes[nid] for nid in ids if nid in self._nodes]

    def get_outgoing(
        self, node_id: str, edge_type: Optional[SemanticEdgeType] = None
    ) -> List[Tuple[SemanticEdgeType, str]]:
        """Return outgoing edges from *node_id*, optionally filtered."""
        with self._lock:
            edges = list(self._outgoing.get(node_id, []))
            if edge_type is not None:
                edges = [(et, tid) for et, tid in edges if et == edge_type]
            return edges

    def get_incoming(
        self, node_id: str, edge_type: Optional[SemanticEdgeType] = None
    ) -> List[Tuple[SemanticEdgeType, str]]:
        """Return incoming edges to *node_id*, optionally filtered."""
        with self._lock:
            edges = list(self._incoming.get(node_id, []))
            if edge_type is not None:
                edges = [(et, sid) for et, sid in edges if et == edge_type]
            return edges

    def node_count(self) -> int:
        """Return total number of nodes."""
        with self._lock:
            return len(self._nodes)

    def edge_count(self) -> int:
        """Return total number of edges."""
        with self._lock:
            return len(self._edges)

    # -- Domain query methods (RequirementGraph compatibility) -----------------

    def get_requirements_for_action(self, action_id: str) -> List[Any]:
        """Get all requirement nodes constraining an action via CONSTRAINS edges."""
        with self._lock:
            incoming = self._incoming.get(action_id, [])
            return [
                self._nodes[src]
                for et, src in incoming
                if et == SemanticEdgeType.CONSTRAINS and src in self._nodes
            ]

    def get_state_vars_read_by(self, node_id: str) -> List[Any]:
        """Get state variable nodes read by a requirement/property via READS edges."""
        with self._lock:
            outgoing = self._outgoing.get(node_id, [])
            return [
                self._nodes[tgt]
                for et, tgt in outgoing
                if et == SemanticEdgeType.READS and tgt in self._nodes
            ]

    def get_coverage_stats(self) -> Dict[str, Any]:
        """Get RFC coverage statistics from the semantic graph."""
        from ivy_lsp.core.analysis.requirement_graph import RequirementNode

        with self._lock:
            reqs = list(self._nodes_by_type.get(RequirementNode, {}).values())
            covered = [
                r
                for r in reqs
                if any(
                    et == SemanticEdgeType.COVERS
                    for et, _ in self._outgoing.get(r.id, [])
                )
            ]
            return {
                "total_requirements": len(reqs),
                "covered": len(covered),
                "uncovered": len(reqs) - len(covered),
            }
