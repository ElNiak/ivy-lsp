"""Tests for the three-tier analysis pipeline orchestrator."""

from ivy_lsp.adapters.null_adapter import (
    NullAstEnrichmentAdapter,
    NullCompilerAdapter,
    NullParserAdapter,
)
from ivy_lsp.adapters.protocols import TypeAnnotation
from ivy_lsp.parsing.parser_session import ParseResult
from ivy_lsp.semantic.analysis_pipeline import AnalysisPipeline
from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel
from ivy_lsp.semantic.nodes import RfcAnnotation, SymbolNode, TypeNode


# ---------------------------------------------------------------------------
# Helper: stub adapters that return controllable data
# ---------------------------------------------------------------------------


class StubParserAdapter:
    """Parser adapter that returns a configurable ParseResult."""

    def __init__(self, success: bool = True) -> None:
        self._success = success

    def parse(self, source: str, filename: str) -> ParseResult:
        if self._success:
            # Return a non-None AST sentinel so enrichment runs
            return ParseResult(ast={"stub": True}, errors=[], success=True, filename=filename)
        return ParseResult(ast=None, errors=[], success=False, filename=filename)


class StubEnrichmentAdapter:
    """Enrichment adapter returning preset TypeAnnotation list."""

    def __init__(self, annotations=None) -> None:
        self._annotations = annotations or []

    def extract_type_info(self, ast, filename, source):
        return list(self._annotations)


# ---------------------------------------------------------------------------
# Tier 1 tests
# ---------------------------------------------------------------------------


class TestTier1:
    """Tier 1 should parse RFC annotations and populate the model."""

    def test_tier1_populates_rfc_annotations(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "require x > 0; # [rfc9000:4.1]\nrequire y > 0;"
        pipeline.run_tier1(source, "test.ivy")

        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 1
        ann = nodes[0]
        assert isinstance(ann, RfcAnnotation)
        assert ann.tags == ["rfc9000:4.1"]
        assert ann.file == "test.ivy"
        assert ann.line == 0

    def test_tier1_multi_annotations(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = (
            "require x; # [rfc9000:4.1]\n"
            "plain code\n"
            "require y; # [rfc9000:8.1, rfc9000:17.2]\n"
        )
        pipeline.run_tier1(source, "multi.ivy")

        nodes = model.get_nodes_in_file("multi.ivy")
        assert len(nodes) == 2
        tags_collected = []
        for n in nodes:
            assert isinstance(n, RfcAnnotation)
            tags_collected.extend(n.tags)
        assert "rfc9000:4.1" in tags_collected
        assert "rfc9000:8.1" in tags_collected
        assert "rfc9000:17.2" in tags_collected

    def test_tier1_no_annotations_produces_empty_model(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "type cid\ntype pkt_num\n"
        pipeline.run_tier1(source, "noann.ivy")

        assert model.node_count() == 0
        assert model.edge_count() == 0

    def test_tier1_replaces_previous_tier1_data(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source_v1 = "require x; # [rfc9000:4.1]\n"
        pipeline.run_tier1(source_v1, "test.ivy")
        assert model.node_count() == 1

        # Second tier1 call with different content replaces old data
        source_v2 = "require y; # [rfc9000:8.1]\nrequire z; # [rfc9000:17.2]\n"
        pipeline.run_tier1(source_v2, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 2
        tags = []
        for n in nodes:
            tags.extend(n.tags)
        assert "rfc9000:4.1" not in tags
        assert "rfc9000:8.1" in tags
        assert "rfc9000:17.2" in tags


# ---------------------------------------------------------------------------
# Tier 2 tests
# ---------------------------------------------------------------------------


class TestTier2:
    """Tier 2 should parse AST and extract type/symbol info."""

    def test_tier2_with_null_adapters_produces_only_annotations(self):
        """Null adapters parse fails -> only RFC annotations end up in model."""
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "require x; # [rfc9000:4.1]\ntype cid\n"
        pipeline.run_tier2(source, "test.ivy")

        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 1
        assert isinstance(nodes[0], RfcAnnotation)

    def test_tier2_creates_type_nodes(self):
        """Stub adapter returning a type annotation should produce TypeNode."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="cid",
            qualified_name="quic.cid",
            sort_name="type",
            line=5,
            is_enum=False,
        )
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "type cid\n"
        pipeline.run_tier2(source, "types.ivy")

        type_nodes = model.get_nodes_by_type(TypeNode)
        assert len(type_nodes) == 1
        assert type_nodes[0].name == "cid"
        assert type_nodes[0].qualified_name == "quic.cid"
        assert type_nodes[0].tier == "tier2"

    def test_tier2_creates_symbol_nodes_for_actions(self):
        """Stub adapter returning action type annotation -> SymbolNode."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="send",
            qualified_name="quic.send",
            sort_name="action",
            arity=2,
            params=["src:cid", "dst:cid"],
            return_sort=None,
            line=10,
            is_enum=False,
        )
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "action send(src:cid, dst:cid)\n"
        pipeline.run_tier2(source, "actions.ivy")

        sym_nodes = model.get_nodes_by_type(SymbolNode)
        assert len(sym_nodes) == 1
        assert sym_nodes[0].name == "send"
        assert sym_nodes[0].kind == "action"
        assert sym_nodes[0].params == ["src:cid", "dst:cid"]
        assert sym_nodes[0].tier == "tier2"

    def test_tier2_creates_has_param_edges(self):
        """Actions with parameters should generate HAS_PARAM edges."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="send",
            qualified_name="quic.send",
            sort_name="action",
            arity=2,
            params=["src:cid", "dst:pkt_num"],
            line=10,
            is_enum=False,
        )
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        pipeline.run_tier2("action send(src:cid, dst:pkt_num)\n", "edges.ivy")

        sym_nodes = model.get_nodes_by_type(SymbolNode)
        assert len(sym_nodes) == 1
        node_id = sym_nodes[0].id
        outgoing = model.get_outgoing(node_id, SemanticEdgeType.HAS_PARAM)
        assert len(outgoing) == 2
        targets = {target for _, target in outgoing}
        assert "cid" in targets
        assert "pkt_num" in targets

    def test_tier2_creates_enum_as_type_node(self):
        """Enum types should be TypeNode regardless of sort_name."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="stream_kind",
            qualified_name="quic.stream_kind",
            sort_name="action",  # even if sort_name is action, is_enum overrides
            is_enum=True,
            variants=["unidir", "bidir"],
            line=3,
        )
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        pipeline.run_tier2("type stream_kind = {unidir, bidir}\n", "enum.ivy")

        type_nodes = model.get_nodes_by_type(TypeNode)
        assert len(type_nodes) == 1
        assert type_nodes[0].is_enum is True
        assert type_nodes[0].variants == ["unidir", "bidir"]
        # Should NOT be a SymbolNode
        assert len(model.get_nodes_by_type(SymbolNode)) == 0

    def test_tier2_includes_rfc_annotations(self):
        """Tier 2 should also produce RFC annotations alongside AST nodes."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="cid",
            qualified_name="cid",
            sort_name="type",
            line=1,
            is_enum=False,
        )
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "type cid\nrequire x; # [rfc9000:4.1]\n"
        pipeline.run_tier2(source, "mixed.ivy")

        all_nodes = model.get_nodes_in_file("mixed.ivy")
        type_nodes = [n for n in all_nodes if isinstance(n, TypeNode)]
        ann_nodes = [n for n in all_nodes if isinstance(n, RfcAnnotation)]
        assert len(type_nodes) == 1
        assert len(ann_nodes) == 1

    def test_tier2_overwrites_tier1(self):
        """Tier 2 data should replace tier 1 data for the same file."""
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "require x; # [rfc9000:4.1]\n"
        pipeline.run_tier1(source, "test.ivy")
        assert model.node_count() == 1

        # tier2 replaces tier1 nodes (same IDs)
        pipeline.run_tier2(source, "test.ivy")
        nodes = model.get_nodes_in_file("test.ivy")
        # Still 1 annotation (null parse fails, so only annotations from tier2)
        assert len(nodes) == 1
        assert isinstance(nodes[0], RfcAnnotation)

    def test_tier2_parse_failure_still_produces_annotations(self):
        """If parse fails, tier2 should still capture RFC annotations."""
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=False),
            enrichment_adapter=StubEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        source = "broken syntax !!!\nrequire x; # [rfc9000:4.1]\n"
        pipeline.run_tier2(source, "broken.ivy")

        nodes = model.get_nodes_in_file("broken.ivy")
        assert len(nodes) == 1
        assert isinstance(nodes[0], RfcAnnotation)


# ---------------------------------------------------------------------------
# Tier 3 tests
# ---------------------------------------------------------------------------


class StubCompilerAdapter:
    """Compiler adapter returning configurable CompileResult."""

    def __init__(self, success: bool = True) -> None:
        self._success = success
        self._callback_called = False

    def compile(self, source: str, filename: str):
        from ivy_lsp.adapters.protocols import CompileResult
        return CompileResult(success=self._success)

    def compile_background(self, source, filename, callback=None):
        result = self.compile(source, filename)
        if callback:
            self._callback_called = True
            callback(result)


class TestTier3:
    """Tier 3 tests -- verify callback behavior."""

    def test_tier3_is_noop_with_null_adapter(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        # Should not raise
        pipeline.run_tier3_background("type cid\n", "test.ivy")
        assert model.node_count() == 0

    def test_tier3_callback_on_success(self):
        model = SemanticModel()
        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        pipeline.run_tier3_background("type cid\n", "test.ivy")
        assert compiler._callback_called

    def test_tier3_callback_on_failure_does_not_update_model(self):
        model = SemanticModel()
        # Pre-populate with tier1 data
        from ivy_lsp.semantic.nodes import RfcAnnotation
        model.add_node(RfcAnnotation(id="test.ivy:0:0", file="test.ivy", line=0, tags=["x"]))

        compiler = StubCompilerAdapter(success=False)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        pipeline.run_tier3_background("bad code\n", "test.ivy")
        # Pre-existing node should still be there (failure doesn't clear)
        assert model.node_count() == 1


# ---------------------------------------------------------------------------
# analyze() orchestration tests
# ---------------------------------------------------------------------------


class TestAnalyzeOrchestration:
    """Verify analyze() dispatches correct tiers based on trigger."""

    def _make_pipeline(self, model):
        return AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

    def test_change_trigger_runs_tier1_and_tier2(self):
        model = SemanticModel()
        pipeline = self._make_pipeline(model)

        source = "require x; # [rfc9000:4.1]\n"
        pipeline.analyze(source, "test.ivy", trigger="change")

        # tier2 overwrites tier1 -> should still have annotation
        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 1
        assert isinstance(nodes[0], RfcAnnotation)

    def test_save_trigger_runs_all_tiers(self):
        model = SemanticModel()
        pipeline = self._make_pipeline(model)

        source = "require x; # [rfc9000:4.1]\n"
        pipeline.analyze(source, "test.ivy", trigger="save")

        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 1  # tier3 is noop

    def test_command_trigger_runs_only_tier3(self):
        model = SemanticModel()
        pipeline = self._make_pipeline(model)

        source = "require x; # [rfc9000:4.1]\n"
        pipeline.analyze(source, "test.ivy", trigger="command")

        # tier3 is noop, tier1/tier2 were NOT run
        assert model.node_count() == 0

    def test_default_trigger_is_change(self):
        model = SemanticModel()
        pipeline = self._make_pipeline(model)

        source = "require x; # [rfc9000:4.1]\n"
        pipeline.analyze(source, "test.ivy")  # no trigger arg -> default "change"

        nodes = model.get_nodes_in_file("test.ivy")
        assert len(nodes) == 1

    def test_analyze_with_multiple_files(self):
        """Analyzing different files should not interfere with each other."""
        model = SemanticModel()
        pipeline = self._make_pipeline(model)

        pipeline.analyze("require x; # [a]\n", "file_a.ivy", trigger="change")
        pipeline.analyze("require y; # [b]\n", "file_b.ivy", trigger="change")

        nodes_a = model.get_nodes_in_file("file_a.ivy")
        nodes_b = model.get_nodes_in_file("file_b.ivy")
        assert len(nodes_a) == 1
        assert len(nodes_b) == 1
        assert nodes_a[0].tags == ["a"]
        assert nodes_b[0].tags == ["b"]
