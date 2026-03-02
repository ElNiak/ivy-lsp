"""Tests for RequirementGraph and its node/edge types.

Covers node creation, graph mutation (add/remove), edge wiring,
and all query methods.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.analysis.requirement_graph import (
    ActionNode,
    EdgeType,
    PropertyNode,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_req(
    file: str,
    line: int,
    kind: str = "require",
    formula: str = "true",
    action: str = "",
    mixin_kind: str = "before",
) -> RequirementNode:
    return RequirementNode(
        id=f"{file}:{line}",
        kind=kind,
        formula_text=formula,
        line=line,
        col=0,
        file=file,
        monitor_action=action,
        mixin_kind=mixin_kind,
    )


def _make_state_var(
    name: str,
    file: str = "/test/vars.ivy",
    line: int = 0,
    is_relation: bool = False,
) -> StateVarNode:
    return StateVarNode(
        id=name,
        name=name.split(".")[-1],
        qualified_name=name,
        file=file,
        line=line,
        is_relation=is_relation,
    )


def _make_action(
    name: str,
    file: str = "/test/actions.ivy",
    line: int = 0,
) -> ActionNode:
    return ActionNode(
        id=name,
        name=name.split(".")[-1],
        qualified_name=name,
        file=file,
        line=line,
    )


def _make_property(
    file: str,
    line: int,
    kind: str = "invariant",
    name: str = "prop",
    formula: str = "true",
) -> PropertyNode:
    return PropertyNode(
        id=f"{file}:{line}",
        kind=kind,
        name=name,
        formula_text=formula,
        file=file,
        line=line,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph():
    """Empty RequirementGraph."""
    return RequirementGraph()


@pytest.fixture
def populated_graph():
    """Graph with two files, several nodes, and edges.

    /test/file_a.ivy:
      - req_a1 (require, line 10) constrains "quic.send"
      - req_a2 (ensure, line 20) constrains "quic.send"
      - state var "sent_pkt" (line 5)
      - action "quic.send" (line 1)

    /test/file_b.ivy:
      - req_b1 (require, line 15) constrains "quic.recv"
      - state var "recv_pkt" (line 3)
      - action "quic.recv" (line 1)
      - property inv1 (invariant, line 30)
    """
    g = RequirementGraph()

    # File A nodes
    req_a1 = _make_req("/test/file_a.ivy", 10, "require", "sent_pkt > 0", "quic.send")
    req_a2 = _make_req("/test/file_a.ivy", 20, "ensure", "sent_pkt = old_val + 1", "quic.send", "after")
    sv_sent = _make_state_var("sent_pkt", "/test/file_a.ivy", 5, is_relation=False)
    act_send = _make_action("quic.send", "/test/file_a.ivy", 1)

    # File B nodes
    req_b1 = _make_req("/test/file_b.ivy", 15, "require", "recv_pkt >= 0", "quic.recv")
    sv_recv = _make_state_var("recv_pkt", "/test/file_b.ivy", 3, is_relation=True)
    act_recv = _make_action("quic.recv", "/test/file_b.ivy", 1)
    inv1 = _make_property("/test/file_b.ivy", 30, "invariant", "inv1", "sent_pkt > 0 & recv_pkt >= 0")

    g.add_requirement(req_a1)
    g.add_requirement(req_a2)
    g.add_requirement(req_b1)
    g.add_state_var(sv_sent)
    g.add_state_var(sv_recv)
    g.add_action(act_send)
    g.add_action(act_recv)
    g.add_property(inv1)

    # Edges
    g.add_edge(req_a1.id, EdgeType.CONSTRAINS, "quic.send")
    g.add_edge(req_a2.id, EdgeType.CONSTRAINS, "quic.send")
    g.add_edge(req_b1.id, EdgeType.CONSTRAINS, "quic.recv")
    g.add_edge(req_a1.id, EdgeType.READS, "sent_pkt")
    g.add_edge(req_a2.id, EdgeType.READS, "sent_pkt")
    g.add_edge(req_b1.id, EdgeType.READS, "recv_pkt")
    g.add_edge(inv1.id, EdgeType.READS, "sent_pkt")
    g.add_edge(inv1.id, EdgeType.READS, "recv_pkt")

    return g


# ---------------------------------------------------------------------------
# 1. Test creating node types
# ---------------------------------------------------------------------------


def test_create_requirement_node():
    node = _make_req("/test/file.ivy", 10, "require", "x > 0", "some_action")
    assert node.id == "/test/file.ivy:10"
    assert node.kind == "require"
    assert node.formula_text == "x > 0"
    assert node.line == 10
    assert node.col == 0
    assert node.file == "/test/file.ivy"
    assert node.monitor_action == "some_action"
    assert node.mixin_kind == "before"
    assert node.bracket_tags == []
    assert node.ast_node is None


def test_create_requirement_node_with_optional_fields():
    node = RequirementNode(
        id="/f:1",
        kind="assert",
        formula_text="y = 0",
        line=1,
        col=5,
        file="/f",
        monitor_action="act",
        mixin_kind="after",
        bracket_tags=["4"],
        ast_node="fake_ast",
    )
    assert node.bracket_tags == ["4"]
    assert node.ast_node == "fake_ast"


def test_create_state_var_node():
    node = _make_state_var("frame.ack.sent_pkt", "/test/vars.ivy", 5, True)
    assert node.id == "frame.ack.sent_pkt"
    assert node.name == "sent_pkt"
    assert node.qualified_name == "frame.ack.sent_pkt"
    assert node.file == "/test/vars.ivy"
    assert node.line == 5
    assert node.is_relation is True
    assert node.params is None


def test_create_action_node():
    node = _make_action("quic.send", "/test/actions.ivy", 12)
    assert node.id == "quic.send"
    assert node.name == "send"
    assert node.qualified_name == "quic.send"
    assert node.file == "/test/actions.ivy"
    assert node.line == 12


def test_create_property_node():
    node = _make_property("/test/props.ivy", 42, "axiom", "symmetry", "r(X,Y) -> r(Y,X)")
    assert node.id == "/test/props.ivy:42"
    assert node.kind == "axiom"
    assert node.name == "symmetry"
    assert node.formula_text == "r(X,Y) -> r(Y,X)"
    assert node.file == "/test/props.ivy"
    assert node.line == 42


# ---------------------------------------------------------------------------
# 2. Test adding nodes to graph
# ---------------------------------------------------------------------------


def test_add_requirement_node(graph):
    req = _make_req("/test/file.ivy", 1)
    graph.add_requirement(req)
    assert "/test/file.ivy:1" in graph.requirements
    assert graph.requirements["/test/file.ivy:1"] is req


def test_add_state_var_node(graph):
    sv = _make_state_var("my_var")
    graph.add_state_var(sv)
    assert "my_var" in graph.state_vars
    assert graph.state_vars["my_var"] is sv


def test_add_action_node(graph):
    act = _make_action("quic.send")
    graph.add_action(act)
    assert "quic.send" in graph.actions
    assert graph.actions["quic.send"] is act


def test_add_property_node(graph):
    prop = _make_property("/test/file.ivy", 5, "invariant", "inv1")
    graph.add_property(prop)
    assert "/test/file.ivy:5" in graph.properties
    assert graph.properties["/test/file.ivy:5"] is prop


def test_add_duplicate_id_overwrites(graph):
    req1 = _make_req("/test/file.ivy", 1, formula="first")
    req2 = _make_req("/test/file.ivy", 1, formula="second")
    graph.add_requirement(req1)
    graph.add_requirement(req2)
    assert graph.requirements["/test/file.ivy:1"].formula_text == "second"


# ---------------------------------------------------------------------------
# 3. Test adding edges and verifying adjacency
# ---------------------------------------------------------------------------


def test_add_edge_stores_triple(graph):
    graph.add_edge("src", EdgeType.READS, "dst")
    assert len(graph.edges) == 1
    assert graph.edges[0] == ("src", EdgeType.READS, "dst")


def test_add_edge_outgoing_adjacency(graph):
    graph.add_edge("src", EdgeType.READS, "dst")
    outgoing = graph._outgoing["src"]
    assert len(outgoing) == 1
    assert outgoing[0] == (EdgeType.READS, "dst")


def test_add_edge_incoming_adjacency(graph):
    graph.add_edge("src", EdgeType.READS, "dst")
    incoming = graph._incoming["dst"]
    assert len(incoming) == 1
    assert incoming[0] == (EdgeType.READS, "src")


def test_add_multiple_edges_same_source(graph):
    graph.add_edge("src", EdgeType.READS, "var1")
    graph.add_edge("src", EdgeType.READS, "var2")
    graph.add_edge("src", EdgeType.CONSTRAINS, "act1")
    assert len(graph._outgoing["src"]) == 3
    targets = {t for _, t in graph._outgoing["src"]}
    assert targets == {"var1", "var2", "act1"}


def test_add_multiple_edges_same_target(graph):
    graph.add_edge("req1", EdgeType.CONSTRAINS, "act")
    graph.add_edge("req2", EdgeType.CONSTRAINS, "act")
    assert len(graph._incoming["act"]) == 2
    sources = {s for _, s in graph._incoming["act"]}
    assert sources == {"req1", "req2"}


def test_edge_types_enum():
    assert EdgeType.READS.value == "reads"
    assert EdgeType.WRITES.value == "writes"
    assert EdgeType.CONSTRAINS.value == "constrains"
    assert EdgeType.DEPENDS_ON.value == "depends_on"
    assert EdgeType.PROPAGATED_FROM.value == "propagated_from"


# ---------------------------------------------------------------------------
# 4. Test get_requirements_for_action
# ---------------------------------------------------------------------------


def test_get_requirements_for_action_returns_constraining_reqs(populated_graph):
    reqs = populated_graph.get_requirements_for_action("quic.send")
    assert len(reqs) == 2
    ids = {r.id for r in reqs}
    assert ids == {"/test/file_a.ivy:10", "/test/file_a.ivy:20"}


def test_get_requirements_for_action_single_req(populated_graph):
    reqs = populated_graph.get_requirements_for_action("quic.recv")
    assert len(reqs) == 1
    assert reqs[0].id == "/test/file_b.ivy:15"


def test_get_requirements_for_action_unknown_action(populated_graph):
    reqs = populated_graph.get_requirements_for_action("nonexistent.action")
    assert reqs == []


def test_get_requirements_for_action_ignores_non_constrains_edges(graph):
    req = _make_req("/test/file.ivy", 1, action="act")
    graph.add_requirement(req)
    graph.add_edge(req.id, EdgeType.READS, "act")
    reqs = graph.get_requirements_for_action("act")
    assert reqs == []


# ---------------------------------------------------------------------------
# 5. Test get_requirements_sharing_state_var
# ---------------------------------------------------------------------------


def test_get_requirements_sharing_state_var(populated_graph):
    reqs = populated_graph.get_requirements_sharing_state_var("sent_pkt")
    assert len(reqs) == 2
    ids = {r.id for r in reqs}
    assert ids == {"/test/file_a.ivy:10", "/test/file_a.ivy:20"}


def test_get_requirements_sharing_state_var_single(populated_graph):
    reqs = populated_graph.get_requirements_sharing_state_var("recv_pkt")
    assert len(reqs) == 1
    assert reqs[0].id == "/test/file_b.ivy:15"


def test_get_requirements_sharing_state_var_unknown(populated_graph):
    reqs = populated_graph.get_requirements_sharing_state_var("unknown_var")
    assert reqs == []


def test_get_requirements_sharing_state_var_excludes_properties(populated_graph):
    """Properties that READS a var should not appear in requirement queries."""
    reqs = populated_graph.get_requirements_sharing_state_var("sent_pkt")
    for r in reqs:
        assert r.id in populated_graph.requirements


# ---------------------------------------------------------------------------
# 6. Test get_active_requirements_for_file
# ---------------------------------------------------------------------------


def test_get_active_requirements_for_file_own_file(populated_graph):
    """Requirements defined in the file itself should be returned."""
    include_graph = SimpleNamespace(
        get_transitive_includes=lambda fp: set()
    )
    reqs = populated_graph.get_active_requirements_for_file(
        "/test/file_a.ivy", include_graph
    )
    assert len(reqs) == 2
    ids = {r.id for r in reqs}
    assert ids == {"/test/file_a.ivy:10", "/test/file_a.ivy:20"}


def test_get_active_requirements_for_file_with_includes(populated_graph):
    """Requirements from transitively included files should be returned."""
    include_graph = SimpleNamespace(
        get_transitive_includes=lambda fp: {"/test/file_b.ivy"}
    )
    reqs = populated_graph.get_active_requirements_for_file(
        "/test/file_a.ivy", include_graph
    )
    assert len(reqs) == 3
    ids = {r.id for r in reqs}
    assert "/test/file_a.ivy:10" in ids
    assert "/test/file_a.ivy:20" in ids
    assert "/test/file_b.ivy:15" in ids


def test_get_active_requirements_for_file_unknown_file(populated_graph):
    """File with no requirements and no includes returns empty list."""
    include_graph = SimpleNamespace(
        get_transitive_includes=lambda fp: set()
    )
    reqs = populated_graph.get_active_requirements_for_file(
        "/test/no_such_file.ivy", include_graph
    )
    assert reqs == []


# ---------------------------------------------------------------------------
# 7. Test remove_file
# ---------------------------------------------------------------------------


def test_remove_file_removes_nodes(populated_graph):
    populated_graph.remove_file("/test/file_a.ivy")
    assert "/test/file_a.ivy:10" not in populated_graph.requirements
    assert "/test/file_a.ivy:20" not in populated_graph.requirements
    assert "sent_pkt" not in populated_graph.state_vars
    assert "quic.send" not in populated_graph.actions


def test_remove_file_preserves_other_file(populated_graph):
    populated_graph.remove_file("/test/file_a.ivy")
    assert "/test/file_b.ivy:15" in populated_graph.requirements
    assert "recv_pkt" in populated_graph.state_vars
    assert "quic.recv" in populated_graph.actions
    assert "/test/file_b.ivy:30" in populated_graph.properties


def test_remove_file_removes_edges(populated_graph):
    populated_graph.remove_file("/test/file_a.ivy")
    # Edges involving file_a nodes should be removed
    for src, _etype, _tgt in populated_graph.edges:
        assert "/test/file_a.ivy" not in src or src not in {
            "/test/file_a.ivy:10",
            "/test/file_a.ivy:20",
        }


def test_remove_file_rebuilds_adjacency(populated_graph):
    populated_graph.remove_file("/test/file_a.ivy")
    # quic.send was from file_a, so no more CONSTRAINS incoming to it
    assert "quic.send" not in populated_graph._incoming
    # quic.recv should still have its CONSTRAINS edge
    reqs = populated_graph.get_requirements_for_action("quic.recv")
    assert len(reqs) == 1


def test_remove_file_noop_for_unknown_file(populated_graph):
    edge_count_before = len(populated_graph.edges)
    req_count_before = len(populated_graph.requirements)
    populated_graph.remove_file("/nonexistent/path.ivy")
    assert len(populated_graph.edges) == edge_count_before
    assert len(populated_graph.requirements) == req_count_before


# ---------------------------------------------------------------------------
# 8. Test add_file_requirements (bulk add with auto CONSTRAINS)
# ---------------------------------------------------------------------------


def test_add_file_requirements_adds_reqs_and_constrains(graph):
    req1 = _make_req("/test/bulk.ivy", 1, "require", "x > 0", "act1")
    req2 = _make_req("/test/bulk.ivy", 5, "ensure", "y = 0", "act2")
    graph.add_file_requirements("/test/bulk.ivy", [req1, req2])

    assert "/test/bulk.ivy:1" in graph.requirements
    assert "/test/bulk.ivy:5" in graph.requirements

    # Should have auto-wired CONSTRAINS edges
    constrains_edges = [
        (s, t) for s, et, t in graph.edges if et == EdgeType.CONSTRAINS
    ]
    assert ("/test/bulk.ivy:1", "act1") in constrains_edges
    assert ("/test/bulk.ivy:5", "act2") in constrains_edges


def test_add_file_requirements_no_constrains_when_no_action(graph):
    req = _make_req("/test/bulk.ivy", 1, "require", "true", action="")
    graph.add_file_requirements("/test/bulk.ivy", [req])

    constrains_edges = [e for e in graph.edges if e[1] == EdgeType.CONSTRAINS]
    assert len(constrains_edges) == 0


def test_add_file_requirements_with_writes(graph):
    req = _make_req("/test/bulk.ivy", 1, "require", "x > 0", "act1")
    writes = [("my_var", "/test/bulk.ivy", 3)]
    graph.add_file_requirements("/test/bulk.ivy", [req], writes=writes)

    write_edges = [(s, t) for s, et, t in graph.edges if et == EdgeType.WRITES]
    assert len(write_edges) == 1
    assert write_edges[0] == ("/test/bulk.ivy:3:write:my_var", "my_var")


def test_add_file_requirements_empty_list(graph):
    graph.add_file_requirements("/test/empty.ivy", [])
    assert len(graph.requirements) == 0
    assert len(graph.edges) == 0


# ---------------------------------------------------------------------------
# 9. Test wire_state_var_edges (formula text matching)
# ---------------------------------------------------------------------------


def test_wire_state_var_edges_links_formula_refs(graph):
    req = _make_req("/test/file.ivy", 1, "require", "sent_pkt > 0 & recv_pkt = 1", "act")
    sv1 = _make_state_var("sent_pkt")
    sv2 = _make_state_var("recv_pkt")
    graph.add_requirement(req)
    graph.add_state_var(sv1)
    graph.add_state_var(sv2)

    known_vars = {"sent_pkt", "recv_pkt"}
    graph.wire_state_var_edges(known_vars)

    reads_targets = {
        t for et, t in graph._outgoing.get(req.id, []) if et == EdgeType.READS
    }
    assert "sent_pkt" in reads_targets
    assert "recv_pkt" in reads_targets


def test_wire_state_var_edges_with_properties(graph):
    prop = _make_property("/test/file.ivy", 10, "invariant", "inv1", "my_var >= 0")
    sv = _make_state_var("my_var")
    graph.add_property(prop)
    graph.add_state_var(sv)

    graph.wire_state_var_edges({"my_var"})

    reads_targets = {
        t for et, t in graph._outgoing.get(prop.id, []) if et == EdgeType.READS
    }
    assert "my_var" in reads_targets


def test_wire_state_var_edges_no_match(graph):
    req = _make_req("/test/file.ivy", 1, "require", "true")
    graph.add_requirement(req)
    graph.wire_state_var_edges({"unknown_var"})
    assert len(graph.edges) == 0


def test_wire_state_var_edges_dotted_name(graph):
    req = _make_req("/test/file.ivy", 1, "require", "frame.ack.sent_pkt > 0")
    sv = _make_state_var("sent_pkt")
    graph.add_requirement(req)
    graph.add_state_var(sv)

    graph.wire_state_var_edges({"sent_pkt"})

    reads_targets = {
        t for et, t in graph._outgoing.get(req.id, []) if et == EdgeType.READS
    }
    assert "sent_pkt" in reads_targets


# ---------------------------------------------------------------------------
# 10. Test wire_dependency_edges (properties sharing state vars with axioms)
# ---------------------------------------------------------------------------


def test_wire_dependency_edges_axiom_to_invariant(graph):
    axiom = _make_property("/test/file.ivy", 1, "axiom", "ax1", "x > 0")
    inv = _make_property("/test/file.ivy", 5, "invariant", "inv1", "x > 0")
    sv = _make_state_var("x")
    graph.add_property(axiom)
    graph.add_property(inv)
    graph.add_state_var(sv)

    # Wire READS edges first (wire_dependency_edges relies on them)
    graph.add_edge(axiom.id, EdgeType.READS, "x")
    graph.add_edge(inv.id, EdgeType.READS, "x")

    graph.wire_dependency_edges()

    depends_edges = [
        (s, t) for s, et, t in graph.edges if et == EdgeType.DEPENDS_ON
    ]
    # Invariant depends on axiom, not the other way around
    assert (inv.id, axiom.id) in depends_edges
    assert (axiom.id, inv.id) not in depends_edges


def test_wire_dependency_edges_no_shared_vars(graph):
    axiom = _make_property("/test/file.ivy", 1, "axiom", "ax1", "x > 0")
    inv = _make_property("/test/file.ivy", 5, "invariant", "inv1", "y > 0")
    graph.add_property(axiom)
    graph.add_property(inv)

    graph.add_edge(axiom.id, EdgeType.READS, "x")
    graph.add_edge(inv.id, EdgeType.READS, "y")

    graph.wire_dependency_edges()

    depends_edges = [e for e in graph.edges if e[1] == EdgeType.DEPENDS_ON]
    assert len(depends_edges) == 0


def test_wire_dependency_edges_two_non_axioms_no_edge(graph):
    """Two invariants sharing a variable should not get DEPENDS_ON edges."""
    inv1 = _make_property("/test/file.ivy", 1, "invariant", "inv1", "x > 0")
    inv2 = _make_property("/test/file.ivy", 5, "invariant", "inv2", "x > 0")
    graph.add_property(inv1)
    graph.add_property(inv2)

    graph.add_edge(inv1.id, EdgeType.READS, "x")
    graph.add_edge(inv2.id, EdgeType.READS, "x")

    graph.wire_dependency_edges()

    depends_edges = [e for e in graph.edges if e[1] == EdgeType.DEPENDS_ON]
    assert len(depends_edges) == 0


def test_wire_dependency_edges_multiple_axioms_and_properties(graph):
    """Multiple properties should each get a DEPENDS_ON edge to a shared axiom."""
    ax = _make_property("/test/file.ivy", 1, "axiom", "ax1", "x > 0")
    inv1 = _make_property("/test/file.ivy", 5, "invariant", "inv1", "x > 0")
    inv2 = _make_property("/test/file.ivy", 10, "conjecture", "conj1", "x > 0")
    graph.add_property(ax)
    graph.add_property(inv1)
    graph.add_property(inv2)

    graph.add_edge(ax.id, EdgeType.READS, "x")
    graph.add_edge(inv1.id, EdgeType.READS, "x")
    graph.add_edge(inv2.id, EdgeType.READS, "x")

    graph.wire_dependency_edges()

    depends_edges = [
        (s, t) for s, et, t in graph.edges if et == EdgeType.DEPENDS_ON
    ]
    assert (inv1.id, ax.id) in depends_edges
    assert (inv2.id, ax.id) in depends_edges
    # Axiom itself should not depend on anything
    axiom_as_source = [s for s, _t in depends_edges if s == ax.id]
    assert len(axiom_as_source) == 0


# ---------------------------------------------------------------------------
# 11. Test get_impact_of_state_var_change
# ---------------------------------------------------------------------------


def test_get_impact_of_state_var_change_groups_by_file(populated_graph):
    impact = populated_graph.get_impact_of_state_var_change("sent_pkt")
    assert "/test/file_a.ivy" in impact
    assert len(impact["/test/file_a.ivy"]) == 2
    ids = {r.id for r in impact["/test/file_a.ivy"]}
    assert ids == {"/test/file_a.ivy:10", "/test/file_a.ivy:20"}


def test_get_impact_of_state_var_change_multiple_files(graph):
    req1 = _make_req("/test/a.ivy", 1, "require", "x > 0", "act")
    req2 = _make_req("/test/b.ivy", 2, "require", "x = 0", "act")
    graph.add_requirement(req1)
    graph.add_requirement(req2)
    graph.add_edge(req1.id, EdgeType.READS, "x")
    graph.add_edge(req2.id, EdgeType.READS, "x")

    impact = graph.get_impact_of_state_var_change("x")
    assert len(impact) == 2
    assert "/test/a.ivy" in impact
    assert "/test/b.ivy" in impact
    assert len(impact["/test/a.ivy"]) == 1
    assert len(impact["/test/b.ivy"]) == 1


def test_get_impact_of_state_var_change_no_readers(populated_graph):
    impact = populated_graph.get_impact_of_state_var_change("nonexistent_var")
    assert impact == {}


# ---------------------------------------------------------------------------
# 12. Test get_requirement_counts_for_action
# ---------------------------------------------------------------------------


def test_get_requirement_counts_for_action(populated_graph):
    counts = populated_graph.get_requirement_counts_for_action("quic.send")
    assert counts["require"] == 1
    assert counts["ensure"] == 1
    assert len(counts) == 2


def test_get_requirement_counts_for_action_single_kind(populated_graph):
    counts = populated_graph.get_requirement_counts_for_action("quic.recv")
    assert counts == {"require": 1}


def test_get_requirement_counts_for_action_unknown(populated_graph):
    counts = populated_graph.get_requirement_counts_for_action("nonexistent")
    assert counts == {}


def test_get_requirement_counts_for_action_many_of_one_kind(graph):
    for i in range(5):
        req = _make_req("/test/file.ivy", i, "require", "true", "act")
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "act")
    counts = graph.get_requirement_counts_for_action("act")
    assert counts == {"require": 5}


# ---------------------------------------------------------------------------
# Additional query coverage
# ---------------------------------------------------------------------------


def test_get_state_vars_read_by(populated_graph):
    vars_read = populated_graph.get_state_vars_read_by("/test/file_a.ivy:10")
    assert len(vars_read) == 1
    assert vars_read[0].id == "sent_pkt"


def test_get_state_vars_read_by_unknown_req(populated_graph):
    vars_read = populated_graph.get_state_vars_read_by("nonexistent:0")
    assert vars_read == []


def test_get_all_requirements_in_file(populated_graph):
    reqs = populated_graph.get_all_requirements_in_file("/test/file_a.ivy")
    assert len(reqs) == 2
    ids = {r.id for r in reqs}
    assert ids == {"/test/file_a.ivy:10", "/test/file_a.ivy:20"}


def test_get_all_requirements_in_file_empty(populated_graph):
    reqs = populated_graph.get_all_requirements_in_file("/nonexistent.ivy")
    assert reqs == []


def test_get_all_state_var_names(populated_graph):
    names = populated_graph.get_all_state_var_names()
    assert names == {"sent_pkt", "recv_pkt"}


def test_get_all_state_var_names_empty(graph):
    assert graph.get_all_state_var_names() == set()


def test_get_properties_depending_on_axiom(graph):
    axiom = _make_property("/test/file.ivy", 1, "axiom", "ax1", "x > 0")
    inv = _make_property("/test/file.ivy", 5, "invariant", "inv1", "x > 0")
    graph.add_property(axiom)
    graph.add_property(inv)
    graph.add_edge(inv.id, EdgeType.DEPENDS_ON, axiom.id)

    deps = graph.get_properties_depending_on_axiom(axiom.id)
    assert len(deps) == 1
    assert deps[0].id == inv.id


def test_get_properties_depending_on_axiom_none(graph):
    axiom = _make_property("/test/file.ivy", 1, "axiom", "ax1", "x > 0")
    graph.add_property(axiom)
    deps = graph.get_properties_depending_on_axiom(axiom.id)
    assert deps == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_graph_queries(graph):
    """All queries on an empty graph return empty results."""
    assert graph.get_requirements_for_action("any") == []
    assert graph.get_requirements_sharing_state_var("any") == []
    assert graph.get_impact_of_state_var_change("any") == {}
    assert graph.get_requirement_counts_for_action("any") == {}
    assert graph.get_all_requirements_in_file("/any") == []
    assert graph.get_all_state_var_names() == set()
    assert graph.get_state_vars_read_by("any") == []
    assert graph.get_properties_depending_on_axiom("any") == []


def test_remove_file_then_query(graph):
    """After removing a file, queries should reflect the removal."""
    req = _make_req("/test/temp.ivy", 1, "require", "true", "act")
    act = _make_action("act", "/test/temp.ivy", 0)
    graph.add_requirement(req)
    graph.add_action(act)
    graph.add_edge(req.id, EdgeType.CONSTRAINS, "act")

    assert len(graph.get_requirements_for_action("act")) == 1
    graph.remove_file("/test/temp.ivy")
    assert graph.get_requirements_for_action("act") == []


def test_remove_file_edges_involving_removed_targets(graph):
    """Edges pointing TO removed nodes should also be cleaned up."""
    sv = _make_state_var("my_var", "/test/removed.ivy", 1)
    req = _make_req("/test/kept.ivy", 1, "require", "my_var > 0")
    graph.add_state_var(sv)
    graph.add_requirement(req)
    graph.add_edge(req.id, EdgeType.READS, "my_var")

    assert len(graph.edges) == 1
    graph.remove_file("/test/removed.ivy")
    # The edge referencing removed state var "my_var" should be gone
    assert len(graph.edges) == 0


# ---------------------------------------------------------------------------
# New tests: populate_actions_from_symbols (C1)
# ---------------------------------------------------------------------------


def _make_ivy_symbol(name, kind, file_path=None, start_line=0):
    """Create a mock IvySymbol-like object."""
    return SimpleNamespace(
        name=name,
        kind=kind,
        range=(start_line, 0, start_line + 1, 0),
        file_path=file_path,
        detail=None,
    )


def test_populate_actions_from_symbols(graph):
    """ActionNodes are created from Function-kind symbols."""
    from lsprotocol.types import SymbolKind

    symbols = [
        _make_ivy_symbol("quic.send", SymbolKind.Function, "/test/actions.ivy", 10),
        _make_ivy_symbol("quic.recv", SymbolKind.Function, "/test/actions.ivy", 20),
        _make_ivy_symbol("my_var", SymbolKind.Variable, "/test/vars.ivy", 5),
    ]
    graph.populate_actions_from_symbols(symbols)

    assert "quic.send" in graph.actions
    assert "quic.recv" in graph.actions
    assert "my_var" not in graph.actions  # Variable, not Function
    assert graph.actions["quic.send"].file == "/test/actions.ivy"
    assert graph.actions["quic.send"].line == 10
    assert graph.actions["quic.send"].name == "send"


def test_populate_actions_includes_monitor_action_refs(graph):
    """ActionNodes are created for monitor_action references in requirements."""
    from lsprotocol.types import SymbolKind

    req = _make_req("/test/file.ivy", 1, "require", "x > 0", "unresolved.action")
    graph.add_requirement(req)

    # No symbols with that name
    symbols = [
        _make_ivy_symbol("other.action", SymbolKind.Function, "/test/other.ivy", 5),
    ]
    graph.populate_actions_from_symbols(symbols)

    # Both the symbol-based and reference-based actions should exist
    assert "other.action" in graph.actions
    assert "unresolved.action" in graph.actions
    assert graph.actions["unresolved.action"].file == "/test/file.ivy"  # fallback uses req's file


def test_populate_actions_does_not_overwrite_existing(graph):
    """Existing ActionNodes should not be overwritten by populate."""
    from lsprotocol.types import SymbolKind

    act = _make_action("quic.send", "/test/original.ivy", 42)
    graph.add_action(act)

    symbols = [
        _make_ivy_symbol("quic.send", SymbolKind.Function, "/test/new.ivy", 99),
    ]
    graph.populate_actions_from_symbols(symbols)

    assert graph.actions["quic.send"].file == "/test/original.ivy"
    assert graph.actions["quic.send"].line == 42


# ---------------------------------------------------------------------------
# New tests: populate_state_vars (C2)
# ---------------------------------------------------------------------------


def test_populate_state_vars(graph):
    """StateVarNodes are created from known_vars set + symbol enrichment."""
    from lsprotocol.types import SymbolKind

    symbols = [
        _make_ivy_symbol("sent_pkt", SymbolKind.Variable, "/test/vars.ivy", 5),
        _make_ivy_symbol("other_fn", SymbolKind.Function, "/test/fns.ivy", 10),
    ]
    known_vars = {"sent_pkt", "unknown_var"}
    graph.populate_state_vars(known_vars, symbols)

    assert "sent_pkt" in graph.state_vars
    assert graph.state_vars["sent_pkt"].file == "/test/vars.ivy"
    assert graph.state_vars["sent_pkt"].line == 5

    assert "unknown_var" in graph.state_vars
    assert graph.state_vars["unknown_var"].file == ""  # no matching symbol


def test_populate_state_vars_does_not_overwrite_existing(graph):
    """Existing StateVarNodes should not be overwritten."""
    sv = _make_state_var("sent_pkt", "/test/original.ivy", 42, True)
    graph.add_state_var(sv)

    from lsprotocol.types import SymbolKind

    symbols = [
        _make_ivy_symbol("sent_pkt", SymbolKind.Variable, "/test/new.ivy", 99),
    ]
    graph.populate_state_vars({"sent_pkt"}, symbols)

    assert graph.state_vars["sent_pkt"].file == "/test/original.ivy"
    assert graph.state_vars["sent_pkt"].is_relation is True


# ---------------------------------------------------------------------------
# New tests: clear_wiring_edges (M1)
# ---------------------------------------------------------------------------


def test_clear_wiring_edges(graph):
    """clear_wiring_edges keeps CONSTRAINS + WRITES + COVERS, removes READS + DEPENDS_ON."""
    graph.add_edge("req1", EdgeType.CONSTRAINS, "act1")
    graph.add_edge("write1", EdgeType.WRITES, "var1")
    graph.add_edge("req1", EdgeType.READS, "var1")
    graph.add_edge("prop1", EdgeType.DEPENDS_ON, "prop2")
    graph.add_edge("req1", EdgeType.COVERS, "rfc1")

    assert len(graph.edges) == 5
    graph.clear_wiring_edges()

    remaining_types = {t for _, t, _ in graph.edges}
    assert EdgeType.CONSTRAINS in remaining_types
    assert EdgeType.WRITES in remaining_types
    assert EdgeType.COVERS in remaining_types
    assert EdgeType.READS not in remaining_types
    assert EdgeType.DEPENDS_ON not in remaining_types
    assert len(graph.edges) == 3


def test_wire_then_rewire_no_duplicates(graph):
    """Calling wire_state_var_edges twice should not create duplicate READS edges."""
    req = _make_req("/test/file.ivy", 1, "require", "sent_pkt > 0", "act")
    sv = _make_state_var("sent_pkt")
    graph.add_requirement(req)
    graph.add_state_var(sv)

    known_vars = {"sent_pkt"}
    graph.wire_state_var_edges(known_vars)

    reads_count_1 = sum(1 for _, t, _ in graph.edges if t == EdgeType.READS)
    assert reads_count_1 == 1

    # Wire again - should clear and re-add, not accumulate
    graph.wire_state_var_edges(known_vars)

    reads_count_2 = sum(1 for _, t, _ in graph.edges if t == EdgeType.READS)
    assert reads_count_2 == 1


# ---------------------------------------------------------------------------
# New test: thread safety (C3)
# ---------------------------------------------------------------------------


def test_thread_safety_concurrent_read_write():
    """Concurrent add_requirement + get_requirements_for_action should not corrupt."""
    import threading

    graph = RequirementGraph()
    act = _make_action("act", "/test/act.ivy", 0)
    graph.add_action(act)

    errors = []

    def writer():
        for i in range(100):
            req = _make_req("/test/file.ivy", i, "require", "true", "act")
            graph.add_requirement(req)
            graph.add_edge(req.id, EdgeType.CONSTRAINS, "act")

    def reader():
        for _ in range(100):
            try:
                reqs = graph.get_requirements_for_action("act")
                # Just verify no exception and return type is correct
                assert isinstance(reqs, list)
            except Exception as e:
                errors.append(e)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread safety errors: {errors}"
    # Verify final state is consistent
    reqs = graph.get_requirements_for_action("act")
    assert len(reqs) == 100


# ---------------------------------------------------------------------------
# C6: add_file_requirements must not accumulate stale edges on re-index
# ---------------------------------------------------------------------------


class TestEdgeAccumulationOnReindex:
    """C6: add_file_requirements must not accumulate stale edges on re-index."""

    def test_reindex_replaces_old_requirements(self):
        g = RequirementGraph()
        r1 = _make_req("/a.ivy", 10, kind="require", action="act1")
        g.add_file_requirements("/a.ivy", [r1])
        assert len(g.requirements) == 1

        # Re-index with shifted line number (different ID)
        r2 = _make_req("/a.ivy", 11, kind="require", action="act1")
        g.add_file_requirements("/a.ivy", [r2])
        assert len(g.requirements) == 1
        assert "/a.ivy:11" in g.requirements
        assert "/a.ivy:10" not in g.requirements

    def test_reindex_clears_stale_constrains_edges(self):
        g = RequirementGraph()
        r1 = _make_req("/a.ivy", 10, action="old_action")
        g.add_file_requirements("/a.ivy", [r1])
        constrains_before = [e for e in g.edges if e[1] == EdgeType.CONSTRAINS]
        assert len(constrains_before) == 1

        r2 = _make_req("/a.ivy", 10, action="new_action")
        g.add_file_requirements("/a.ivy", [r2])
        constrains_after = [e for e in g.edges if e[1] == EdgeType.CONSTRAINS]
        assert len(constrains_after) == 1
        assert constrains_after[0][2] == "new_action"

    def test_reindex_clears_stale_write_edges(self):
        g = RequirementGraph()
        r1 = _make_req("/a.ivy", 10, action="act")
        g.add_file_requirements("/a.ivy", [r1], writes=[("var1", "/a.ivy", 15)])
        writes_before = [e for e in g.edges if e[1] == EdgeType.WRITES]
        assert len(writes_before) == 1

        r2 = _make_req("/a.ivy", 10, action="act")
        g.add_file_requirements("/a.ivy", [r2], writes=[("var2", "/a.ivy", 16)])
        writes_after = [e for e in g.edges if e[1] == EdgeType.WRITES]
        assert len(writes_after) == 1


# ---------------------------------------------------------------------------
# Caching: get_active_requirements_for_file version-stamped cache
# ---------------------------------------------------------------------------


class _SpyIncludeGraph:
    """Include graph mock that counts calls to get_transitive_includes."""

    def __init__(self, result: set | None = None):
        self._result = result if result is not None else set()
        self.call_count = 0

    def get_transitive_includes(self, filepath: str) -> set:
        self.call_count += 1
        return self._result


class TestRequirementGraphCaching:
    """Tests for the version-stamped cache on get_active_requirements_for_file."""

    def test_repeated_query_uses_cache(self):
        """Second call with unchanged graph should not re-traverse."""
        graph = RequirementGraph()
        graph.add_action(ActionNode(
            id="act1", name="act1", qualified_name="act1",
            file="/test.ivy", line=1,
        ))
        graph.add_requirement(RequirementNode(
            id="/test.ivy:5", kind="require", formula_text="x>0",
            line=5, col=0, file="/test.ivy",
            monitor_action="act1", mixin_kind="before",
        ))
        graph.add_edge("/test.ivy:5", EdgeType.CONSTRAINS, "act1")

        spy = _SpyIncludeGraph()
        r1 = graph.get_active_requirements_for_file("/test.ivy", spy)
        r2 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert r1 == r2
        assert len(r1) == 1
        # The include graph should only be consulted once (cache hit on 2nd call)
        assert spy.call_count == 1

    def test_cache_invalidated_after_add_requirement(self):
        """Adding a requirement should invalidate cached queries."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        r1 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r1) == 0
        assert spy.call_count == 1

        graph.add_requirement(RequirementNode(
            id="/test.ivy:5", kind="require", formula_text="x>0",
            line=5, col=0, file="/test.ivy",
            monitor_action="act1", mixin_kind="before",
        ))

        r2 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r2) == 1
        # Must have re-traversed because the graph version changed
        assert spy.call_count == 2

    def test_cache_invalidated_after_add_action(self):
        """add_action bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 1

        graph.add_action(ActionNode(
            id="act1", name="act1", qualified_name="act1",
            file="/test.ivy", line=1,
        ))

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 2

    def test_cache_invalidated_after_add_state_var(self):
        """add_state_var bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 1

        graph.add_state_var(StateVarNode(
            id="var1", name="var1", qualified_name="var1",
            file="/test.ivy", line=1,
        ))

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 2

    def test_cache_invalidated_after_add_property(self):
        """add_property bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 1

        graph.add_property(PropertyNode(
            id="/test.ivy:10", kind="invariant", name="inv1",
            formula_text="true", file="/test.ivy", line=10,
        ))

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 2

    def test_cache_invalidated_after_remove_file(self):
        """remove_file bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        graph.add_requirement(RequirementNode(
            id="/test.ivy:5", kind="require", formula_text="x>0",
            line=5, col=0, file="/test.ivy",
            monitor_action="act1", mixin_kind="before",
        ))
        spy = _SpyIncludeGraph()

        r1 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r1) == 1
        assert spy.call_count == 1

        graph.remove_file("/test.ivy")

        r2 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r2) == 0
        assert spy.call_count == 2

    def test_cache_invalidated_after_add_file_requirements(self):
        """add_file_requirements bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        r1 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r1) == 0
        assert spy.call_count == 1

        req = _make_req("/test.ivy", 5, "require", "x>0", "act1")
        graph.add_file_requirements("/test.ivy", [req])

        r2 = graph.get_active_requirements_for_file("/test.ivy", spy)
        assert len(r2) == 1
        assert spy.call_count == 2

    def test_cache_invalidated_after_add_edge(self):
        """add_edge bumps version so stale cache is not reused."""
        graph = RequirementGraph()
        spy = _SpyIncludeGraph()

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 1

        graph.add_edge("src", EdgeType.READS, "dst")

        graph.get_active_requirements_for_file("/test.ivy", spy)
        assert spy.call_count == 2

    def test_different_files_cached_independently(self):
        """Queries for different files should be cached independently."""
        graph = RequirementGraph()
        graph.add_requirement(_make_req("/a.ivy", 1, action="act"))
        graph.add_requirement(_make_req("/b.ivy", 2, action="act"))

        spy = _SpyIncludeGraph()

        ra = graph.get_active_requirements_for_file("/a.ivy", spy)
        rb = graph.get_active_requirements_for_file("/b.ivy", spy)

        assert len(ra) == 1
        assert len(rb) == 1
        assert ra[0].file == "/a.ivy"
        assert rb[0].file == "/b.ivy"
        # Both queries needed their own traversal
        assert spy.call_count == 2

        # Now both should be cached
        graph.get_active_requirements_for_file("/a.ivy", spy)
        graph.get_active_requirements_for_file("/b.ivy", spy)
        assert spy.call_count == 2  # no additional calls
