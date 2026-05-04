"""Tests for coverage_hints.compute_coverage_hints -- action-centric unguarded writes."""

from unittest.mock import MagicMock

from ivy_lsp.core.analysis.requirement_graph import (
    ActionNode,
    EdgeType,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.core.coverage_hints import compute_coverage_hints
from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

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
    """Action-centric unguarded-write hint names both the action and the variable."""
    g = _make_graph_with_unguarded_action()
    hints = compute_coverage_hints(g, FILEPATH)
    # Section 2b emits action-centric hints (message contains the action name).
    action_hints = [
        h
        for h in hints
        if h.code == "ivy.action.unguardedWrite" and "'send'" in h.message
    ]
    assert len(action_hints) == 1
    h = action_hints[0]
    assert h.line == 10
    assert "'sent_pkt'" in h.message
    from lsprotocol import types as lsp

    assert h.severity == lsp.DiagnosticSeverity.Hint


def test_duplicate_writes_deduplicated():
    """Same var written twice in one action appears only once in the action-centric message."""
    g = _make_graph_with_unguarded_action()
    # Add a second write of 'sent_pkt' at a different line in the same action
    g.add_edge(f"{FILEPATH}:14:write:sent_pkt", EdgeType.WRITES, "sent_pkt")
    hints = compute_coverage_hints(g, FILEPATH)
    # Section 2b emits action-centric hints; filter by action name in message.
    action_hints = [
        h
        for h in hints
        if h.code == "ivy.action.unguardedWrite" and "'send'" in h.message
    ]
    assert len(action_hints) == 1
    # 'sent_pkt' should appear exactly once in the message
    assert action_hints[0].message.count("'sent_pkt'") == 1


def test_guarded_action_no_diagnostic():
    """Action whose written vars are all guarded produces no diagnostic."""
    g = _make_graph_with_guarded_action()
    hints = compute_coverage_hints(g, FILEPATH)
    action_hints = [h for h in hints if h.code == "ivy.action.unguardedWrite"]
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
    # Section 2b emits action-centric hints; filter by action name in message.
    action_hints = [
        h
        for h in hints
        if h.code == "ivy.action.unguardedWrite"
        and ("'alpha'" in h.message or "'beta'" in h.message)
    ]
    assert len(action_hints) == 2

    alpha_hint = [h for h in action_hints if "'alpha'" in h.message]
    beta_hint = [h for h in action_hints if "'beta'" in h.message]
    assert len(alpha_hint) == 1
    assert "'var_a'" in alpha_hint[0].message
    assert len(beta_hint) == 1
    assert "'var_b'" in beta_hint[0].message


# -- Deduplication tests (structural vs graph-based) -----------------------


def test_structural_unguarded_action_suppressed_with_graph():
    """When graph is available, structural 'ivy.action.unguardedWrite' is filtered out."""
    source = "#lang ivy1.7\naction send(S:cid) = {\n    sent(S) := true;\n}\n"
    indexer = MagicMock()
    indexer.requirement_graph = RequirementGraph()
    indexer.resolver = MagicMock()
    indexer.resolver.resolve = MagicMock(return_value=None)
    indexer.resolver._partition_staging = {}

    diags = compute_diagnostics(
        parser=None,
        source=source,
        filepath="/fake/test.ivy",
        indexer=indexer,
    )
    codes = [d.code for d in diags if d.code is not None]
    assert "ivy.action.unguardedWrite" not in codes


def test_structural_unguarded_action_present_without_graph():
    """Without graph, structural 'ivy.action.unguardedWrite' fires as fallback."""
    source = "#lang ivy1.7\naction send(S:cid) = {\n    sent(S) := true;\n}\n"

    diags = compute_diagnostics(
        parser=None,
        source=source,
        filepath="/fake/test.ivy",
        indexer=None,
    )
    codes = [d.code for d in diags if d.code is not None]
    assert "ivy.action.unguardedWrite" in codes


# -- Cross-path dedup tests (Phase 1 review issue 7) -----------------------


def test_unguarded_write_deduplicated_across_paths():
    """Var-centric emit is suppressed when action-centric covers the same var.

    The user sees ONE diagnostic at the action line, not two squiggles for
    the same (action, var) pair.
    """
    g = _make_graph_with_unguarded_action()
    hints = compute_coverage_hints(g, FILEPATH)
    # Total ivy.action.unguardedWrite count: exactly one (action-centric).
    unguarded = [h for h in hints if h.code == "ivy.action.unguardedWrite"]
    assert len(unguarded) == 1, (
        f"expected exactly one unguardedWrite after dedup, "
        f"got {len(unguarded)}: {[h.message for h in unguarded]}"
    )
    h = unguarded[0]
    # Anchored at the action declaration (line 10), not the var (line 5).
    assert h.line == 10
    # Action-centric message names the action; var-centric does not.
    assert "'send'" in h.message
    assert "without a 'require' precondition" in h.message


def test_var_centric_fires_for_writes_outside_in_file_actions():
    """Var-centric path still fires for writes outside any in-file action.

    Covers writes attributed to a cross-file action. Without this fall-through
    the dedup would silently drop coverage for cross-file writes.
    """
    g = RequirementGraph()
    g.add_state_var(
        StateVarNode(
            id="shared_state",
            name="shared_state",
            qualified_name="shared_state",
            file=FILEPATH,
            line=4,
            is_relation=True,
        )
    )
    # Action declared in a DIFFERENT file. The action-centric loop only
    # iterates actions whose .file == filepath, so it cannot pick this up.
    OTHER_FILE = "/fake/other.ivy"
    g.add_action(
        ActionNode(
            id="cross_file_writer",
            name="cross_file_writer",
            qualified_name="cross_file_writer",
            file=OTHER_FILE,
            line=20,
        )
    )
    # Write edge attributed to the OTHER file at line 22. Even if the
    # action-centric loop ran on FILEPATH, the source prefix wouldn't
    # match `FILEPATH:` so the write wouldn't be bucketed.
    g.add_edge(f"{OTHER_FILE}:22:write:shared_state", EdgeType.WRITES, "shared_state")

    hints = compute_coverage_hints(g, FILEPATH)
    unguarded = [h for h in hints if h.code == "ivy.action.unguardedWrite"]
    # The var-centric path should fire because the action-centric path
    # cannot reach this cross-file write.
    assert len(unguarded) == 1, (
        f"expected one unguardedWrite from var-centric fall-through, "
        f"got {len(unguarded)}: {[h.message for h in unguarded]}"
    )
    h = unguarded[0]
    # Anchored at the var declaration (line 4) since the action lives
    # in another file.
    assert h.line == 4
    assert "'shared_state'" in h.message
    assert "written but" in h.message


# --- Phase 5 cluster 5.2b: precise-range assertions ---


def test_range_precision_no_monitor_with_columns():
    """ActionNode populated with start_col/end_col emits precise range."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="send",
            name="send",
            qualified_name="send",
            file=FILEPATH,
            line=10,
            start_col=4,
            end_col=8,
        )
    )
    hints = compute_coverage_hints(g, FILEPATH)
    nm = next(h for h in hints if h.code == "ivy.action.noMonitor")
    assert nm.line == 10
    assert nm.character == 4
    assert nm.end_line == 10
    assert nm.end_character == 8


def test_range_precision_no_monitor_falls_back_when_columns_zero():
    """ActionNode without column info preserves prior fall-through behaviour."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="send",
            name="send",
            qualified_name="send",
            file=FILEPATH,
            line=10,
            # start_col=0, end_col=0 (defaults)
        )
    )
    hints = compute_coverage_hints(g, FILEPATH)
    nm = next(h for h in hints if h.code == "ivy.action.noMonitor")
    assert nm.line == 10
    assert nm.character == 0
    # No precise end_character → falls through to _DEFAULT_END_COLUMN at to_lsp().
    assert nm.end_character is None


def test_range_precision_unused_state_var_with_columns():
    """StateVarNode populated with start_col/end_col emits precise range."""
    g = RequirementGraph()
    g.add_state_var(
        StateVarNode(
            id="orphan",
            name="orphan",
            qualified_name="orphan",
            file=FILEPATH,
            line=5,
            start_col=9,
            end_col=15,
        )
    )
    hints = compute_coverage_hints(g, FILEPATH)
    h = next(d for d in hints if d.code == "ivy.state.unusedStateVar")
    assert h.line == 5
    assert h.character == 9
    assert h.end_character == 15


def test_range_precision_dead_guard_uses_req_col():
    """RequirementNode with col + end_col emits precise range."""
    g = RequirementGraph()
    g.add_action(
        ActionNode(
            id="dead",
            name="dead",
            qualified_name="dead",
            file=FILEPATH,
            line=5,
        )
    )
    req = RequirementNode(
        id=f"{FILEPATH}:7:require",
        kind="require",
        formula_text="false",
        line=7,
        col=4,
        file=FILEPATH,
        monitor_action="dead",
        mixin_kind="before",
        end_col=20,
    )
    g.add_requirement(req)
    g.add_edge(req.id, EdgeType.CONSTRAINS, "dead")
    hints = compute_coverage_hints(g, FILEPATH)
    h = next(d for d in hints if d.code == "ivy.require.deadGuard")
    assert h.line == 7
    assert h.character == 4
    assert h.end_character == 20
