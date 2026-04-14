"""Tests for coverage_hints.compute_coverage_hints -- action-centric unguarded writes."""

from ivy_lsp.core.analysis.requirement_graph import (
    ActionNode,
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.core.coverage_hints import compute_coverage_hints

FILEPATH = "/fake/test.ivy"


def _make_graph_with_unguarded_action():
    """Graph: action 'send' at line 10 writes 'sent_pkt' (unguarded)."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="send",
            name="send",
            qualified_name="send",
            file=FILEPATH,
            line=10,
        )
    )
    g.add_state_var(
        StateVarNode(
            id="sent_pkt",
            name="sent_pkt",
            qualified_name="sent_pkt",
            file=FILEPATH,
            line=5,
            is_relation=True,
        )
    )
    # Write edge: line 12 inside action send's body
    g.add_edge(f"{FILEPATH}:12:write:sent_pkt", EdgeType.WRITES, "sent_pkt")
    return g


def _make_graph_with_guarded_action():
    """Graph: action 'send' writes 'sent_pkt', but a requirement reads it."""
    g = _make_graph_with_unguarded_action()
    req = RequirementNode(
        id=f"{FILEPATH}:15:require",
        kind="require",
        formula_text="sent_pkt(S)",
        line=15,
        col=0,
        file=FILEPATH,
        monitor_action="send",
        mixin_kind="before",
    )
    g.add_requirement(req)
    g.add_edge(req.id, EdgeType.CONSTRAINS, "send")
    g.add_edge(req.id, EdgeType.READS, "sent_pkt")
    return g


def test_unguarded_action_names_variables():
    """Action with unguarded write produces diagnostic naming the variable."""
    g = _make_graph_with_unguarded_action()
    hints = compute_coverage_hints(g, FILEPATH)
    action_hints = [h for h in hints if h["code"] == "ivy.action.unguardedWrite"]
    assert len(action_hints) == 1
    h = action_hints[0]
    assert h["line"] == 10
    assert "'send'" in h["message"]
    assert "'sent_pkt'" in h["message"]
    assert h["severity"] == "hint"


def test_guarded_action_no_diagnostic():
    """Action whose written vars are all guarded produces no diagnostic."""
    g = _make_graph_with_guarded_action()
    hints = compute_coverage_hints(g, FILEPATH)
    action_hints = [h for h in hints if h["code"] == "ivy.action.unguardedWrite"]
    assert len(action_hints) == 0


def test_multiple_actions_line_bucketing():
    """Writes are attributed to the correct action via line ranges."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="alpha",
            name="alpha",
            qualified_name="alpha",
            file=FILEPATH,
            line=5,
        )
    )
    g.add_action(
        ActionNode(
            id="beta",
            name="beta",
            qualified_name="beta",
            file=FILEPATH,
            line=20,
        )
    )
    g.add_state_var(
        StateVarNode(
            id="var_a",
            name="var_a",
            qualified_name="var_a",
            file=FILEPATH,
            line=2,
            is_relation=True,
        )
    )
    g.add_state_var(
        StateVarNode(
            id="var_b",
            name="var_b",
            qualified_name="var_b",
            file=FILEPATH,
            line=3,
            is_relation=True,
        )
    )
    # var_a written at line 10 (inside alpha), var_b at line 25 (inside beta)
    g.add_edge(f"{FILEPATH}:10:write:var_a", EdgeType.WRITES, "var_a")
    g.add_edge(f"{FILEPATH}:25:write:var_b", EdgeType.WRITES, "var_b")

    hints = compute_coverage_hints(g, FILEPATH)
    action_hints = [h for h in hints if h["code"] == "ivy.action.unguardedWrite"]
    assert len(action_hints) == 2

    alpha_hint = [h for h in action_hints if "'alpha'" in h["message"]]
    beta_hint = [h for h in action_hints if "'beta'" in h["message"]]
    assert len(alpha_hint) == 1
    assert "'var_a'" in alpha_hint[0]["message"]
    assert len(beta_hint) == 1
    assert "'var_b'" in beta_hint[0]["message"]
