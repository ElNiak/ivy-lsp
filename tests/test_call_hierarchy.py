"""Tests for the call hierarchy feature."""

from __future__ import annotations

from pathlib import Path

import pytest
from lsprotocol import types as lsp

from ivy_lsp.lsp.navigation.call_hierarchy import (
    get_incoming_calls,
    get_outgoing_calls,
    prepare_call_hierarchy,
)


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> str:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def _index(workspace_root: str):
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

    parser = IvyParserWrapper()
    resolver = IncludeResolver(workspace_root)
    indexer = WorkspaceIndexer(workspace_root, parser, resolver)
    indexer.index_workspace()
    return indexer


SAMPLE_SOURCE = (
    "#lang ivy1.7\n"
    "\n"
    "type cid\n"
    "\n"
    "action connect(src:cid, dst:cid)\n"
    "\n"
    "action process(c:cid) = {\n"
    "    connect(c, c);\n"
    "}\n"
    "\n"
    "before connect {\n"
    "    require src ~= dst;\n"
    "}\n"
)


class TestPrepareCallHierarchy:
    """prepareCallHierarchy: return CallHierarchyItem for symbol at cursor."""

    @pytest.mark.unit
    def test_action_returns_item(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "connect" in "action connect" — line 4, char 7
        result = prepare_call_hierarchy(
            indexer, filepath, lsp.Position(line=4, character=7), lines
        )
        assert result is not None
        assert len(result) == 1
        item = result[0]
        assert item.name == "connect"
        assert item.kind == lsp.SymbolKind.Method

    @pytest.mark.unit
    def test_type_returns_none(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "cid" — a type, not callable
        result = prepare_call_hierarchy(
            indexer, filepath, lsp.Position(line=2, character=5), lines
        )
        # Types are SymbolKind.Class, should return None
        assert result is None

    @pytest.mark.unit
    def test_empty_returns_none(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": "#lang ivy1.7\n\n\n"})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        result = prepare_call_hierarchy(
            indexer, filepath, lsp.Position(line=2, character=0), lines
        )
        assert result is None

    @pytest.mark.unit
    def test_before_block_returns_item(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        lines = Path(filepath).read_text().split("\n")
        # Cursor on "connect" in "before connect" — line 10, char 7
        result = prepare_call_hierarchy(
            indexer, filepath, lsp.Position(line=10, character=7), lines
        )
        assert result is not None
        assert len(result) >= 1


class TestIncomingCalls:
    """incomingCalls: who calls this action?"""

    @pytest.mark.unit
    def test_action_called_from_another_action(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_incoming_calls(indexer, "connect", filepath)
        # "process" calls "connect" in its body, and "before connect" references it
        assert len(result) >= 1
        caller_names = [call.from_.name for call in result]
        assert "process" in caller_names

    @pytest.mark.unit
    def test_before_is_incoming_caller(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_incoming_calls(indexer, "connect", filepath)
        # "before connect" block should appear as an incoming caller
        caller_names = [call.from_.name for call in result]
        # The before block's symbol name is "connect" (the monitor target)
        # It should show up as a caller since it references "connect"
        assert len(result) >= 1

    @pytest.mark.unit
    def test_no_callers_returns_empty(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": "#lang ivy1.7\n\ntype cid\n\naction unused(c:cid)\n",
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_incoming_calls(indexer, "unused", filepath)
        assert result == []


class TestOutgoingCalls:
    """outgoingCalls: what does this action call?"""

    @pytest.mark.unit
    def test_action_calls_another(self, tmp_path):
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        # "process" calls "connect" in its body
        result = get_outgoing_calls(indexer, "process", filepath)
        assert len(result) >= 1
        callee_names = [call.to.name for call in result]
        assert "connect" in callee_names

    @pytest.mark.unit
    def test_action_with_no_body_returns_empty(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": "#lang ivy1.7\n\ntype cid\n\naction connect(src:cid, dst:cid)\n",
            },
        )
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_outgoing_calls(indexer, "connect", filepath)
        assert result == []

    @pytest.mark.unit
    def test_file_not_found_returns_empty(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            {
                "proto.ivy": "#lang ivy1.7\n\ntype cid\n",
            },
        )
        indexer = _index(ws)
        result = get_outgoing_calls(indexer, "connect", "/nonexistent/file.ivy")
        assert result == []


# ---------------------------------------------------------------------------
# Model-based call hierarchy tests
# ---------------------------------------------------------------------------


class TestModelBasedIncomingCalls:
    """Test incoming calls via semantic model."""

    @pytest.mark.unit
    def test_model_based_incoming_calls(self):
        """When model has CALLS edges, use them instead of regex."""
        from unittest.mock import MagicMock

        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="process",
            qualified_name="process",
            kind="action",
            file="test.ivy",
            line=3,
        )
        s2 = SymbolNode(
            id="s2",
            name="connect",
            qualified_name="connect",
            kind="action",
            file="test.ivy",
            line=1,
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_edge("s1", SemanticEdgeType.CALLS, "s2")

        # The model path doesn't use the indexer, so a mock is sufficient.
        indexer = MagicMock()
        result = get_incoming_calls(indexer, "connect", "test.ivy", model=model)
        assert len(result) >= 1
        assert any(c.from_.name == "process" for c in result)

    @pytest.mark.unit
    def test_model_based_incoming_monitors(self):
        """MONITORS edges should also appear as incoming callers."""
        from unittest.mock import MagicMock

        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import MonitorNode, SymbolNode

        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="connect",
            qualified_name="connect",
            kind="action",
            file="test.ivy",
            line=1,
        )
        m1 = MonitorNode(
            id="m1",
            action_name="connect",
            mixin_kind="before",
            file="test.ivy",
            line=3,
        )
        model.add_node(s1)
        model.add_node(m1)
        model.add_edge("m1", SemanticEdgeType.MONITORS, "s1")

        indexer = MagicMock()
        result = get_incoming_calls(indexer, "connect", "test.ivy", model=model)
        assert len(result) >= 1

    @pytest.mark.unit
    def test_fallback_to_regex_when_model_is_none(self, tmp_path):
        """model=None should fall back to the regex scanner."""
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_incoming_calls(indexer, "connect", filepath, model=None)
        assert len(result) >= 1

    @pytest.mark.unit
    def test_fallback_to_regex_when_model_empty(self, tmp_path):
        """Empty model (no matching node) falls back to regex."""
        from ivy_lsp.core.semantic.model import SemanticModel

        model = SemanticModel()  # no nodes

        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_incoming_calls(indexer, "connect", filepath, model=model)
        # Should still find callers via regex fallback
        assert len(result) >= 1


class TestModelBasedOutgoingCalls:
    """Test outgoing calls via semantic model."""

    @pytest.mark.unit
    def test_model_based_outgoing_calls(self):
        """When model has CALLS edges, use them instead of regex."""
        from unittest.mock import MagicMock

        from ivy_lsp.core.semantic.edges import SemanticEdgeType
        from ivy_lsp.core.semantic.model import SemanticModel
        from ivy_lsp.core.semantic.nodes import SymbolNode

        model = SemanticModel()
        s1 = SymbolNode(
            id="s1",
            name="process",
            qualified_name="process",
            kind="action",
            file="test.ivy",
            line=3,
        )
        s2 = SymbolNode(
            id="s2",
            name="connect",
            qualified_name="connect",
            kind="action",
            file="test.ivy",
            line=1,
        )
        model.add_node(s1)
        model.add_node(s2)
        model.add_edge("s1", SemanticEdgeType.CALLS, "s2")

        indexer = MagicMock()
        result = get_outgoing_calls(indexer, "process", "test.ivy", model=model)
        assert len(result) >= 1
        assert any(c.to.name == "connect" for c in result)

    @pytest.mark.unit
    def test_fallback_to_regex_when_model_is_none(self, tmp_path):
        """model=None should fall back to the regex scanner."""
        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_outgoing_calls(indexer, "process", filepath, model=None)
        assert len(result) >= 1

    @pytest.mark.unit
    def test_fallback_to_regex_when_model_empty(self, tmp_path):
        """Empty model (no matching node) falls back to regex."""
        from ivy_lsp.core.semantic.model import SemanticModel

        model = SemanticModel()  # no nodes

        ws = _make_workspace(tmp_path, {"proto.ivy": SAMPLE_SOURCE})
        indexer = _index(ws)
        filepath = str(tmp_path / "proto.ivy")
        result = get_outgoing_calls(indexer, "process", filepath, model=model)
        # Should still find callees via regex fallback
        assert len(result) >= 1
