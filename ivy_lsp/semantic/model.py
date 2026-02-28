"""Thread-safe unified semantic model for the Ivy LSP analysis pipeline.

The SemanticModel stores all node and edge data produced by the three
analysis tiers.  LSP features and MCP tools query this single model
instead of maintaining their own data structures.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple, Type

from ivy_lsp.semantic.edges import SemanticEdgeType

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
        self._lock = threading.RLock()

        # Primary storage
        self._nodes: Dict[str, Any] = {}
        self._nodes_by_type: Dict[type, Dict[str, Any]] = defaultdict(dict)
        self._nodes_by_file: Dict[str, Set[str]] = defaultdict(set)
        self._node_tiers: Dict[str, str] = {}  # node_id -> tier

        # Edges – use a set to prevent duplicate accumulation
        self._edges: Set[Tuple[str, SemanticEdgeType, str]] = set()
        self._outgoing: Dict[str, List[Tuple[SemanticEdgeType, str]]] = defaultdict(
            list
        )
        self._incoming: Dict[str, List[Tuple[SemanticEdgeType, str]]] = defaultdict(
            list
        )

    # -- Mutation -----------------------------------------------------------

    def add_node(self, node: Any) -> None:
        """Add or replace a node in the model."""
        node_id = node.id
        with self._lock:
            self._nodes[node_id] = node
            self._nodes_by_type[type(node)][node_id] = node
            file_attr = getattr(node, "file", None)
            if file_attr:
                self._nodes_by_file[file_attr].add(node_id)
            tier = getattr(node, "tier", None)
            if tier:
                self._node_tiers[node_id] = tier

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
                self._node_tiers.pop(nid, None)
            self._edges = {
                (src, etype, dst)
                for src, etype, dst in self._edges
                if src not in node_ids and dst not in node_ids
            }
            self._rebuild_adjacency()

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
                self._node_tiers.pop(nid, None)
                file_set = self._nodes_by_file.get(filepath)
                if file_set:
                    file_set.discard(nid)

            # Remove old edges involving removed ids
            if ids_to_remove:
                self._edges = {
                    (src, etype, dst)
                    for src, etype, dst in self._edges
                    if src not in ids_to_remove and dst not in ids_to_remove
                }

            # Add new nodes (skip if existing node at higher tier)
            for node in nodes:
                nid = node.id
                existing_tier = self._node_tiers.get(nid)
                if existing_tier and tier_rank.get(existing_tier, 0) > new_rank:
                    continue  # preserve higher-tier node
                self._nodes[nid] = node
                self._nodes_by_type[type(node)][nid] = node
                self._nodes_by_file[filepath].add(nid)
                self._node_tiers[nid] = tier

            # Add new edges (set prevents duplicates)
            for src, etype, dst in edges:
                self._edges.add((src, etype, dst))

            self._rebuild_adjacency()

    def _rebuild_adjacency(self) -> None:
        """Rebuild adjacency indices from the edge list."""
        self._outgoing = defaultdict(list)
        self._incoming = defaultdict(list)
        for src, etype, dst in self._edges:
            self._outgoing[src].append((etype, dst))
            self._incoming[dst].append((etype, src))

    # -- Queries ------------------------------------------------------------

    def get_node(self, node_id: str) -> Optional[Any]:
        """Return a node by id, or None."""
        with self._lock:
            return self._nodes.get(node_id)

    def get_nodes_by_type(self, node_type: Type) -> List[Any]:
        """Return all nodes of a given type."""
        with self._lock:
            return list(self._nodes_by_type.get(node_type, {}).values())

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
