"""Tests for ivy_lsp.analysis.formula_analyzer module.

Covers both text-based extraction (extract_state_var_references_text) and
AST-based extraction (extract_state_var_references).  Uses lightweight mock
objects instead of real Ivy AST nodes so the test suite stays independent of
the Ivy compiler.
"""

from __future__ import annotations

import pytest

from ivy_lsp.analysis.formula_analyzer import (
    extract_state_var_references,
    extract_state_var_references_text,
)

# ---------------------------------------------------------------------------
# Lightweight mock AST helpers
# ---------------------------------------------------------------------------


class MockNode:
    """Minimal stand-in for an Ivy AST node.

    Supports optional ``relname``, ``rep``, and ``args`` attributes so we can
    exercise every branch of the recursive walker without importing Ivy.
    """

    def __init__(
        self,
        *,
        relname: str | None = None,
        rep: str | None = None,
        args: tuple | list | None = None,
    ):
        if relname is not None:
            self.relname = relname
        if rep is not None:
            self.rep = rep
        if args is not None:
            self.args = args


# ===================================================================
# extract_state_var_references_text  (text-based / light-mode)
# ===================================================================


class TestExtractStateVarReferencesText:
    """Text-based tokenization + matching against known_vars."""

    # 1. Simple match
    def test_simple_match(self):
        result = extract_state_var_references_text("sent_pkt(C, N)", {"sent_pkt"})
        assert result == ["sent_pkt"]

    # 2. Dotted match (full path present in known_vars)
    def test_dotted_match_full_path(self):
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N)", {"frame.ack.sent_pkt"}
        )
        assert result == ["frame.ack.sent_pkt"]

    # 3. Multiple vars referenced in one formula
    def test_multiple_vars(self):
        result = extract_state_var_references_text(
            "sent_pkt(C, N) & ack_received(C)",
            {"sent_pkt", "ack_received"},
        )
        assert set(result) == {"sent_pkt", "ack_received"}
        assert len(result) == 2

    # 4. No match
    def test_no_match(self):
        result = extract_state_var_references_text(
            "foo(X, Y)", {"sent_pkt", "ack_received"}
        )
        assert result == []

    # 5. Empty formula text
    def test_empty_formula_text(self):
        result = extract_state_var_references_text("", {"sent_pkt"})
        assert result == []

    # 6. Empty known_vars
    def test_empty_known_vars(self):
        result = extract_state_var_references_text("sent_pkt(C, N)", set())
        assert result == []

    # 7. Suffix segment matching: known_vars has "sent_pkt", formula has
    #    "frame.ack.sent_pkt" -- the function should match via suffix.
    def test_suffix_segment_matching(self):
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N)", {"sent_pkt"}
        )
        assert "sent_pkt" in result

    # 8. Deduplication: same var referenced multiple times
    def test_deduplication(self):
        result = extract_state_var_references_text(
            "sent_pkt(C, N) | sent_pkt(D, M)", {"sent_pkt"}
        )
        assert result == ["sent_pkt"]

    # 9. Complex formula with operators
    def test_complex_formula_with_operators(self):
        result = extract_state_var_references_text(
            "X ~= Y & sent_pkt(C,N) -> ack_received(C)",
            {"sent_pkt", "ack_received"},
        )
        assert set(result) == {"sent_pkt", "ack_received"}
        assert len(result) == 2

    # -- additional edge cases --

    def test_none_formula_text(self):
        """None is falsy, should return empty list."""
        # The function checks ``if not formula_text``, so None works.
        result = extract_state_var_references_text(None, {"sent_pkt"})  # type: ignore[arg-type]
        assert result == []

    def test_suffix_middle_segment_matching(self):
        """Known var 'ack.sent_pkt' matches middle-to-end suffix of 'frame.ack.sent_pkt'.

        Suffix matching allows inner segments to match against longer dotted paths.
        """
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N)", {"ack.sent_pkt"}
        )
        assert "ack.sent_pkt" in result

    def test_individual_part_matching(self):
        """Each individual dotted segment is also checked against known_vars."""
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N)", {"frame"}
        )
        assert "frame" in result

    def test_no_partial_identifier_match(self):
        """'pkt' should NOT match inside 'sent_pkt' as a standalone token.

        The regex tokenizer extracts whole identifiers, so partial matches are excluded.
        """
        result = extract_state_var_references_text("sent_pkt(C, N)", {"pkt"})
        # 'pkt' is not a standalone token in 'sent_pkt', so no match expected.
        assert "pkt" not in result

    def test_underscore_prefix_identifier(self):
        result = extract_state_var_references_text(
            "_internal_state(X)", {"_internal_state"}
        )
        assert result == ["_internal_state"]

    def test_numeric_suffix_identifier(self):
        result = extract_state_var_references_text("pkt_num0(X)", {"pkt_num0"})
        assert result == ["pkt_num0"]

    def test_preserves_first_seen_order(self):
        """Result order follows the order of first appearance in the text."""
        result = extract_state_var_references_text(
            "beta(X) & alpha(Y) & gamma(Z)",
            {"alpha", "beta", "gamma"},
        )
        assert result == ["beta", "alpha", "gamma"]

    def test_both_full_and_suffix_deduplication(self):
        """Duplicate matches from standalone and suffix are deduplicated.

        If 'sent_pkt' appears both as a standalone token and as a suffix
        of 'frame.sent_pkt', it should only appear once.
        """
        result = extract_state_var_references_text(
            "sent_pkt(X) & frame.sent_pkt(Y)", {"sent_pkt"}
        )
        assert result == ["sent_pkt"]

    def test_full_dotted_and_suffix_both_in_known_vars(self):
        """Full dotted path match triggers continue, skipping suffix processing.

        When the full dotted path matches as a token, the `continue`
        statement skips suffix and individual-part processing.  So only the
        full match is returned even though 'sent_pkt' is also in known_vars.
        """
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N)",
            {"frame.ack.sent_pkt", "sent_pkt"},
        )
        # Full match triggers `continue`, so suffix 'sent_pkt' is NOT reached
        assert result == ["frame.ack.sent_pkt"]

    def test_full_dotted_and_suffix_in_separate_tokens(self):
        """Full dotted path and plain occurrence as separate tokens are both found.

        When the full dotted path and a plain occurrence both appear as
        separate tokens, both are included in the result.
        """
        result = extract_state_var_references_text(
            "frame.ack.sent_pkt(C, N) & sent_pkt(X)",
            {"frame.ack.sent_pkt", "sent_pkt"},
        )
        assert "frame.ack.sent_pkt" in result
        assert "sent_pkt" in result


# ===================================================================
# extract_state_var_references  (AST-based / full mode)
# ===================================================================


class TestExtractStateVarReferences:
    """AST-node walker matching .relname and .rep against known_vars."""

    # 1. Simple relname match
    def test_simple_relname_match(self):
        node = MockNode(relname="sent_pkt")
        result = extract_state_var_references(node, {"sent_pkt", "other"})
        assert result == ["sent_pkt"]

    # 2. Nested args -- child nodes also walked
    def test_nested_args(self):
        child = MockNode(relname="ack_received")
        parent = MockNode(relname="sent_pkt", args=[child])
        result = extract_state_var_references(parent, {"sent_pkt", "ack_received"})
        assert result == ["sent_pkt", "ack_received"]

    # 3. rep attribute match
    def test_rep_attribute(self):
        node = MockNode(rep="connection_state")
        result = extract_state_var_references(node, {"connection_state"})
        assert result == ["connection_state"]

    # 4. None node
    def test_none_node(self):
        result = extract_state_var_references(None, {"sent_pkt"})
        assert result == []

    # 5. No matching vars
    def test_no_matching_vars(self):
        node = MockNode(relname="unknown_thing")
        result = extract_state_var_references(node, {"sent_pkt"})
        assert result == []

    # -- additional edge cases --

    def test_empty_known_vars(self):
        node = MockNode(relname="sent_pkt")
        result = extract_state_var_references(node, set())
        assert result == []

    def test_deduplication_across_tree(self):
        """Same relname in two branches should appear only once."""
        left = MockNode(relname="sent_pkt")
        right = MockNode(relname="sent_pkt")
        root = MockNode(args=[left, right])
        result = extract_state_var_references(root, {"sent_pkt"})
        assert result == ["sent_pkt"]

    def test_relname_and_rep_on_same_node(self):
        """Both relname and rep are checked on the same node."""
        node = MockNode(relname="alpha", rep="beta")
        result = extract_state_var_references(node, {"alpha", "beta"})
        assert set(result) == {"alpha", "beta"}
        assert len(result) == 2

    def test_relname_and_rep_same_value(self):
        """If relname and rep resolve to the same string, it should be deduplicated.

        Only one entry should appear in the result list.
        """
        node = MockNode(relname="same_var", rep="same_var")
        result = extract_state_var_references(node, {"same_var"})
        assert result == ["same_var"]

    def test_deep_nesting(self):
        """Walk through multiple levels of nesting."""
        leaf = MockNode(relname="deep_var")
        mid = MockNode(args=[leaf])
        root = MockNode(args=[mid])
        result = extract_state_var_references(root, {"deep_var"})
        assert result == ["deep_var"]

    def test_node_without_relname_or_rep(self):
        """A node with no relname or rep should be silently skipped."""
        node = MockNode()
        result = extract_state_var_references(node, {"anything"})
        assert result == []

    def test_node_with_empty_string_relname(self):
        """Empty string relname is falsy, should not match."""
        node = MockNode(relname="")
        result = extract_state_var_references(node, {""})
        assert result == []

    def test_preserves_first_seen_order(self):
        """Order follows depth-first traversal (parent before children, left before right).

        The result list preserves first-seen order during the tree walk.
        """
        c1 = MockNode(relname="beta")
        c2 = MockNode(relname="gamma")
        root = MockNode(relname="alpha", args=[c1, c2])
        result = extract_state_var_references(root, {"alpha", "beta", "gamma"})
        assert result == ["alpha", "beta", "gamma"]

    def test_node_with_empty_args_tuple(self):
        """A node with an empty args tuple should not cause errors."""
        node = MockNode(relname="var", args=())
        result = extract_state_var_references(node, {"var"})
        assert result == ["var"]

    def test_mixed_relname_and_rep_across_tree(self):
        """One node uses relname, another uses rep -- both matched."""
        n1 = MockNode(relname="sent_pkt")
        n2 = MockNode(rep="ack_received")
        root = MockNode(args=[n1, n2])
        result = extract_state_var_references(root, {"sent_pkt", "ack_received"})
        assert set(result) == {"sent_pkt", "ack_received"}
        assert len(result) == 2

    def test_known_vars_not_mutated(self):
        """The function should not modify the caller's known_vars set."""
        known = {"sent_pkt", "other"}
        known_copy = known.copy()
        node = MockNode(relname="sent_pkt")
        extract_state_var_references(node, known)
        assert known == known_copy
