"""Tests for code lens feature (ivy_lsp.lsp.ui.code_lens)."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from lsprotocol import types as lsp

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.core.analysis.requirement_graph import (
    EdgeType,
    PropertyNode,
    RequirementGraph,
    RequirementNode,
    StateVarNode,
)
from ivy_lsp.lsp.ui.code_lens import compute_code_lenses

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_indexer(graph=None, include_graph=None, resolver=None):
    """Build a mock indexer with the required attributes."""
    indexer = MagicMock()
    indexer.requirement_graph = graph
    indexer.include_graph = include_graph
    indexer.resolver = resolver
    return indexer


def _abs(name: str) -> str:
    """Return an absolute path for a virtual test file."""
    return os.path.abspath(name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeCodeLensesBasic:
    """Basic behaviour and edge cases for compute_code_lenses."""

    def test_no_indexer_returns_empty(self):
        """When indexer is None, no lenses are returned."""
        result = compute_code_lenses(None, "test.ivy", "before foo.step {\n}")
        assert result == []

    def test_indexer_without_graph_returns_empty(self):
        """When indexer has no requirement_graph attribute, no lenses."""
        indexer = MagicMock(spec=[])  # no attributes at all
        del indexer.requirement_graph  # ensure getattr returns None
        result = compute_code_lenses(indexer, "test.ivy", "before foo.step {\n}")
        assert result == []

    def test_empty_graph_no_lenses(self):
        """An empty graph produces no lenses even with matching source."""
        graph = RequirementGraph()
        indexer = _make_indexer(graph=graph)
        source = "before foo.step {\n    require true;\n}\n"
        result = compute_code_lenses(indexer, "test.ivy", source)
        assert result == []


class TestMonitorLenses:
    """Test code lenses on before/after/around blocks."""

    def test_before_block_lens(self):
        """A before block with matching requirements produces a lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:1",
            kind="require",
            formula_text="x ~= x",
            line=1,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "foo.step")

        indexer = _make_indexer(graph=graph)
        source = "before foo.step {\n    require x ~= x;\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        lens = lenses[0]
        assert isinstance(lens, lsp.CodeLens)
        assert lens.range.start.line == 0
        assert "require" in lens.command.title.lower() or "1" in lens.command.title

    def test_after_block_lens_with_ensure(self):
        """An after block with an ensure requirement shows the kind in title."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:2",
            kind="ensure",
            formula_text="true",
            line=2,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="after",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "foo.step")

        indexer = _make_indexer(graph=graph)
        source = "after foo.step {\n    ensure true;\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        assert "ensure" in lenses[0].command.title.lower()

    def test_monitor_lens_with_state_var_reads(self):
        """Monitor lens shows state var read count when READS edges exist."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:1",
            kind="require",
            formula_text="connected(X,Y)",
            line=1,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "foo.step")
        graph.add_edge(req.id, EdgeType.READS, "connected")

        sv = StateVarNode(
            id="connected",
            name="connected",
            qualified_name="connected",
            file=filepath,
            line=0,
            is_relation=True,
        )
        graph.add_state_var(sv)

        indexer = _make_indexer(graph=graph)
        source = "before foo.step {\n    require connected(X,Y);\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        title = lenses[0].command.title
        assert "reads" in title.lower() and "state var" in title.lower()

    def test_multiple_monitors_multiple_lenses(self):
        """Each monitor block gets its own lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        for action in ("foo.step", "bar.step"):
            req = RequirementNode(
                id=f"{filepath}:0:{action}",
                kind="require",
                formula_text="true",
                line=0,
                col=0,
                file=filepath,
                monitor_action=action,
                mixin_kind="before",
            )
            graph.add_requirement(req)
            graph.add_edge(req.id, EdgeType.CONSTRAINS, action)

        indexer = _make_indexer(graph=graph)
        source = "before foo.step {\n    require true;\n}\nbefore bar.step {\n    require true;\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) == 2


class TestStateVarLenses:
    """Test code lenses on relation/function/individual declarations."""

    def test_relation_read_by_requirements(self):
        """A relation with readers gets a 'read by N requirements' lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        sv = StateVarNode(
            id="connected",
            name="connected",
            qualified_name="connected",
            file=filepath,
            line=0,
            is_relation=True,
        )
        graph.add_state_var(sv)

        req1 = RequirementNode(
            id=f"{filepath}:5",
            kind="require",
            formula_text="connected(X,Y)",
            line=5,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        req2 = RequirementNode(
            id="/other/file.ivy:10",
            kind="ensure",
            formula_text="connected(A,B)",
            line=10,
            col=0,
            file="/other/file.ivy",
            monitor_action="bar.step",
            mixin_kind="after",
        )
        graph.add_requirement(req1)
        graph.add_requirement(req2)
        graph.add_edge(req1.id, EdgeType.READS, "connected")
        graph.add_edge(req2.id, EdgeType.READS, "connected")

        indexer = _make_indexer(graph=graph)
        source = "relation connected(X:cid, Y:cid)\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        title = lenses[0].command.title
        assert "read by" in title.lower()
        assert "2" in title  # 2 requirements
        assert "2 files" in title  # 2 different files

    def test_relation_no_readers_no_lens(self):
        """A relation with no readers produces no lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        sv = StateVarNode(
            id="lonely",
            name="lonely",
            qualified_name="lonely",
            file=filepath,
            line=0,
            is_relation=True,
        )
        graph.add_state_var(sv)

        indexer = _make_indexer(graph=graph)
        source = "relation lonely(X:t)\n"
        lenses = compute_code_lenses(indexer, filepath, source)
        assert len(lenses) == 0

    def test_function_declaration_lens(self):
        """A function declaration with readers gets a lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:5",
            kind="require",
            formula_text="count > 0",
            line=5,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.READS, "count")

        indexer = _make_indexer(graph=graph)
        source = "function count(X:t) : nat\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        assert "read by" in lenses[0].command.title.lower()


class TestPropertyLenses:
    """Test code lenses on axiom/property/invariant/conjecture declarations."""

    def test_axiom_with_shared_state(self):
        """An axiom at the correct line sharing state produces a lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        # State variable that both property and requirement read
        sv = StateVarNode(
            id="r",
            name="r",
            qualified_name="r",
            file=filepath,
            line=0,
            is_relation=True,
        )
        graph.add_state_var(sv)

        prop = PropertyNode(
            id=f"{filepath}:0",
            kind="axiom",
            name="sym",
            formula_text="r(X,Y) -> r(Y,X)",
            file=filepath,
            line=0,
        )
        graph.add_property(prop)
        graph.add_edge(prop.id, EdgeType.READS, "r")

        # A requirement also reads "r"
        req = RequirementNode(
            id=f"{filepath}:5",
            kind="require",
            formula_text="r(A,B)",
            line=5,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.READS, "r")

        indexer = _make_indexer(graph=graph)
        source = "axiom [sym] r(X,Y) -> r(Y,X)\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        title = lenses[0].command.title
        assert "shares state" in title.lower() or "requirement" in title.lower()

    def test_property_no_match_when_line_differs(self):
        """A property node at a different line produces no lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        prop = PropertyNode(
            id=f"{filepath}:99",
            kind="axiom",
            name="sym",
            formula_text="r(X,Y) -> r(Y,X)",
            file=filepath,
            line=99,  # does not match line 0 of the source
        )
        graph.add_property(prop)

        indexer = _make_indexer(graph=graph)
        source = "axiom [sym] r(X,Y) -> r(Y,X)\n"
        lenses = compute_code_lenses(indexer, filepath, source)
        assert len(lenses) == 0

    def test_invariant_lens(self):
        """An invariant at the correct line with shared state produces a lens."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        # State variable shared by property and requirement
        sv = StateVarNode(
            id="connected",
            name="connected",
            qualified_name="connected",
            file=filepath,
            line=0,
            is_relation=True,
        )
        graph.add_state_var(sv)

        prop = PropertyNode(
            id=f"{filepath}:0",
            kind="invariant",
            name="inv1",
            formula_text="connected(X,Y) -> connected(Y,X)",
            file=filepath,
            line=0,
        )
        graph.add_property(prop)
        graph.add_edge(prop.id, EdgeType.READS, "connected")

        req = RequirementNode(
            id=f"{filepath}:10",
            kind="require",
            formula_text="connected(A,B)",
            line=10,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.READS, "connected")

        indexer = _make_indexer(graph=graph)
        source = "invariant [inv1] connected(X,Y) -> connected(Y,X)\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1


class TestIncludeLenses:
    """Test code lenses on include directives."""

    def test_include_with_inherited_requirements(self):
        """An include line shows inherited requirement count."""
        filepath = _abs("main.ivy")
        other_file = _abs("types.ivy")
        graph = RequirementGraph()

        # Requirement in the included file
        req = RequirementNode(
            id=f"{other_file}:5",
            kind="require",
            formula_text="x > 0",
            line=5,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()

        resolver = MagicMock()
        resolver.resolve.return_value = other_file

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include quic_types\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        title = lenses[0].command.title
        assert "brings" in title.lower()
        assert "1" in title  # 1 inherited requirement

    def test_include_no_inherited_requirements_no_lens(self):
        """When include brings 0 new requirements, no lens is shown."""
        filepath = _abs("main.ivy")
        other_file = _abs("types.ivy")
        graph = RequirementGraph()

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()

        resolver = MagicMock()
        resolver.resolve.return_value = other_file  # resolves but has 0 reqs

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include quic_types\n"
        lenses = compute_code_lenses(indexer, filepath, source)
        assert len(lenses) == 0

    def test_no_include_graph_skips_include_lenses(self):
        """When indexer has no _include_graph, include lenses are skipped."""
        filepath = _abs("main.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id="/other.ivy:1",
            kind="require",
            formula_text="true",
            line=1,
            col=0,
            file="/other.ivy",
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        indexer = _make_indexer(graph=graph, include_graph=None)
        source = "include quic_types\n"
        lenses = compute_code_lenses(indexer, filepath, source)
        # No include lenses because include_graph is None
        include_lenses = [
            l for l in lenses if "brings" in (l.command.title or "").lower()
        ]
        assert len(include_lenses) == 0

    def test_multiple_includes_each_get_lens(self):
        """Multiple includes show per-file counts, not the same total."""
        filepath = _abs("main.ivy")
        types_file = _abs("types.ivy")
        utils_file = _abs("utils.ivy")
        graph = RequirementGraph()

        # 3 requirements in types.ivy
        for i in range(3):
            req = RequirementNode(
                id=f"{types_file}:{i}",
                kind="require",
                formula_text=f"cond_{i}",
                line=i,
                col=0,
                file=types_file,
                monitor_action="foo.step",
                mixin_kind="before",
            )
            graph.add_requirement(req)

        # 1 requirement in utils.ivy
        req_u = RequirementNode(
            id=f"{utils_file}:0",
            kind="require",
            formula_text="util_cond",
            line=0,
            col=0,
            file=utils_file,
            monitor_action="bar.step",
            mixin_kind="before",
        )
        graph.add_requirement(req_u)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = set()

        resolver = MagicMock()
        resolver.resolve.side_effect = lambda name, _: {
            "types": types_file,
            "utils": utils_file,
        }.get(name)

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include types\ninclude utils\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        include_lenses = [l for l in lenses if "brings" in l.command.title.lower()]
        assert len(include_lenses) == 2
        # First include (types) shows 3, second (utils) shows 1
        assert "3" in include_lenses[0].command.title
        assert "1" in include_lenses[1].command.title

    def test_include_unresolvable_no_lens(self):
        """When the resolver cannot find a file, no lens is generated."""
        filepath = _abs("main.ivy")
        graph = RequirementGraph()

        include_graph = MagicMock()
        resolver = MagicMock()
        resolver.resolve.return_value = None  # unresolvable

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include missing_module\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        include_lenses = [
            l for l in lenses if "brings" in (l.command.title or "").lower()
        ]
        assert len(include_lenses) == 0

    def test_include_no_resolver_no_lens(self):
        """When resolver is None, include lenses degrade gracefully."""
        filepath = _abs("main.ivy")
        other_file = _abs("types.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{other_file}:0",
            kind="require",
            formula_text="x > 0",
            line=0,
            col=0,
            file=other_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = {other_file}

        indexer = _make_indexer(graph=graph, include_graph=include_graph, resolver=None)
        source = "include types\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        include_lenses = [
            l for l in lenses if "brings" in (l.command.title or "").lower()
        ]
        assert len(include_lenses) == 0

    def test_include_transitive_requirements_counted(self):
        """Transitive deps of an include are counted in its lens."""
        filepath = _abs("main.ivy")
        types_file = _abs("types.ivy")
        base_file = _abs("base.ivy")
        graph = RequirementGraph()

        # 1 requirement directly in types.ivy
        req_t = RequirementNode(
            id=f"{types_file}:0",
            kind="require",
            formula_text="t > 0",
            line=0,
            col=0,
            file=types_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req_t)

        # 2 requirements in base.ivy (transitively included by types.ivy)
        for i in range(2):
            req_b = RequirementNode(
                id=f"{base_file}:{i}",
                kind="require",
                formula_text=f"base_{i}",
                line=i,
                col=0,
                file=base_file,
                monitor_action="bar.step",
                mixin_kind="before",
            )
            graph.add_requirement(req_b)

        include_graph = MagicMock()
        include_graph.get_transitive_includes.return_value = {base_file}

        resolver = MagicMock()
        resolver.resolve.return_value = types_file

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include types\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        include_lenses = [l for l in lenses if "brings" in l.command.title.lower()]
        assert len(include_lenses) == 1
        assert "3" in include_lenses[0].command.title  # 1 own + 2 transitive

    def test_include_shared_dependency_deduplicated(self):
        """Shared transitive deps are only counted for the first include."""
        filepath = _abs("main.ivy")
        types_file = _abs("types.ivy")
        utils_file = _abs("utils.ivy")
        base_file = _abs("base.ivy")
        graph = RequirementGraph()

        # types.ivy: 1 own requirement
        req_t = RequirementNode(
            id=f"{types_file}:0",
            kind="require",
            formula_text="t > 0",
            line=0,
            col=0,
            file=types_file,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req_t)

        # utils.ivy: 1 own requirement
        req_u = RequirementNode(
            id=f"{utils_file}:0",
            kind="require",
            formula_text="u > 0",
            line=0,
            col=0,
            file=utils_file,
            monitor_action="bar.step",
            mixin_kind="before",
        )
        graph.add_requirement(req_u)

        # base.ivy: 2 requirements (shared transitive dep)
        for i in range(2):
            req_b = RequirementNode(
                id=f"{base_file}:{i}",
                kind="require",
                formula_text=f"base_{i}",
                line=i,
                col=0,
                file=base_file,
                monitor_action="baz.step",
                mixin_kind="before",
            )
            graph.add_requirement(req_b)

        include_graph = MagicMock()
        # Both types and utils transitively include base
        include_graph.get_transitive_includes.side_effect = lambda f: {
            types_file: {base_file},
            utils_file: {base_file},
        }.get(f, set())

        resolver = MagicMock()
        resolver.resolve.side_effect = lambda name, _: {
            "types": types_file,
            "utils": utils_file,
        }.get(name)

        indexer = _make_indexer(
            graph=graph, include_graph=include_graph, resolver=resolver
        )
        source = "include types\ninclude utils\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        include_lenses = [l for l in lenses if "brings" in l.command.title.lower()]
        assert len(include_lenses) == 2
        # types claims: own (1) + base (2) = 3
        assert "3" in include_lenses[0].command.title
        # utils claims: own (1) only, base already claimed = 1
        assert "1" in include_lenses[1].command.title


class TestLensRange:
    """Verify that lens ranges cover the correct source lines."""

    def test_monitor_lens_range_matches_source_line(self):
        """The lens range starts at the correct line for a monitor block."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:3",
            kind="require",
            formula_text="true",
            line=3,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.CONSTRAINS, "foo.step")

        indexer = _make_indexer(graph=graph)
        # The 'before' is on line 2 (0-indexed)
        source = "# comment\n# comment\nbefore foo.step {\n    require true;\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        assert lenses[0].range.start.line == 2


class TestClickableCodeLenses:
    """Verify that code lens commands include ivy.showActionRequirements."""

    def test_monitor_lens_has_command(self):
        """Monitor code lenses should use ivy.showActionRequirements command."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:1",
            kind="require",
            formula_text="x > 0",
            line=1,
            col=0,
            file=filepath,
            monitor_action="send",
            mixin_kind="before",
        )
        graph.add_file_requirements(filepath, [req])

        indexer = _make_indexer(graph=graph)
        source = "before send {\n    require x > 0\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        monitor_lens = lenses[0]
        assert monitor_lens.command is not None
        assert monitor_lens.command.command == "ivy.showActionRequirements"

    def test_monitor_lens_has_action_name_in_arguments(self):
        """Monitor code lens arguments should contain the action name."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:1",
            kind="require",
            formula_text="x > 0",
            line=1,
            col=0,
            file=filepath,
            monitor_action="send",
            mixin_kind="before",
        )
        graph.add_file_requirements(filepath, [req])

        indexer = _make_indexer(graph=graph)
        source = "before send {\n    require x > 0\n}\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        monitor_lens = lenses[0]
        assert monitor_lens.command.arguments is not None
        assert "send" in monitor_lens.command.arguments

    def test_state_var_lens_has_command(self):
        """State var code lenses should use ivy.showActionRequirements command."""
        filepath = _abs("test.ivy")
        graph = RequirementGraph()

        req = RequirementNode(
            id=f"{filepath}:5",
            kind="require",
            formula_text="connected(X,Y)",
            line=5,
            col=0,
            file=filepath,
            monitor_action="foo.step",
            mixin_kind="before",
        )
        graph.add_requirement(req)
        graph.add_edge(req.id, EdgeType.READS, "connected")

        indexer = _make_indexer(graph=graph)
        source = "relation connected(X:cid, Y:cid)\n"
        lenses = compute_code_lenses(indexer, filepath, source)

        assert len(lenses) >= 1
        sv_lens = lenses[0]
        assert sv_lens.command is not None
        assert sv_lens.command.command == "ivy.showActionRequirements"
        assert sv_lens.command.arguments is not None
        assert "connected" in sv_lens.command.arguments


class TestRegisterHandler:
    """Tests for the async handler registered by code_lens.register()."""

    def _make_server_and_handler(self):
        from ivy_lsp.lsp.ui.code_lens import register

        server = MagicMock()
        registered = {}

        def fake_feature(method):
            def decorator(fn):
                registered[method] = fn
                return fn

            return decorator

        server.feature = fake_feature
        register(server)
        return server, registered.get(lsp.TEXT_DOCUMENT_CODE_LENS)

    @pytest.mark.asyncio
    async def test_code_lens_disabled_returns_empty(self):
        server, handler = self._make_server_and_handler()
        assert handler is not None
        server._code_lens_enabled = False
        params = MagicMock()
        result = await handler(params)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_indexer_returns_empty(self):
        server, handler = self._make_server_and_handler()
        server._code_lens_enabled = True
        server.indexer = None
        params = MagicMock()
        params.text_document.uri = "file:///test.ivy"
        doc = MagicMock()
        doc.source = "before foo {\n}\n"
        server.workspace.get_text_document.return_value = doc
        result = await handler(params)
        assert result == []


class TestComputeCodeLensesErrorPaths:
    """Verify error handling in compute_code_lenses.

    Note: compute_code_lenses does NOT catch exceptions itself --
    the handler in register() catches them. These tests verify
    that errors propagate correctly for the handler to catch.
    """

    def test_attribute_error_propagates(self):
        """AttributeError from graph propagates (handler will catch it)."""
        indexer = MagicMock()
        indexer.requirement_graph = MagicMock()
        indexer.requirement_graph.get_requirements_for_action.side_effect = (
            AttributeError("bad attr")
        )
        indexer.include_graph = None
        indexer.resolver = None

        with pytest.raises(AttributeError, match="bad attr"):
            compute_code_lenses(indexer, "test.ivy", "before foo {\n}\n")

    def test_none_graph_returns_empty(self):
        """A None graph (no _requirement_graph attr) yields empty list."""
        indexer = MagicMock(spec=[])
        result = compute_code_lenses(indexer, "test.ivy", "before foo {\n}\n")
        assert result == []
