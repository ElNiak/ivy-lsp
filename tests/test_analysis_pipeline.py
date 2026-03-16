"""Tests for the three-tier analysis pipeline orchestrator."""

import unittest.mock as mock

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

    def test_tier3_track_state_false_does_not_set_running_flag(self):
        """When track_state=False, _tier3_running should NOT be set."""
        model = SemanticModel()
        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        pipeline.run_tier3_background("type cid\n", "test.ivy", track_state=False)
        assert pipeline._tier3.running is False
        assert pipeline._tier3.current_file is None

    def test_tier3_track_state_false_still_records_result(self):
        """When track_state=False, _record_tier3_result() should still be called."""
        model = SemanticModel()
        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        pipeline.run_tier3_background("type cid\n", "test.ivy", track_state=False)
        state = pipeline.get_pipeline_state()
        assert state["tier3FileCount"] == 1
        assert state["tier3Succeeded"] == 1

    def test_tier3_track_state_true_sets_running_flag(self):
        """When track_state=True (default), _tier3_running is managed normally."""
        model = SemanticModel()
        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        # After synchronous completion, running should be False again
        pipeline.run_tier3_background("type cid\n", "test.ivy", track_state=True)
        assert pipeline._tier3.running is False

    def test_tier3_track_state_false_failure_does_not_touch_flags(self):
        """When track_state=False and compilation fails, flags stay untouched."""
        model = SemanticModel()
        compiler = StubCompilerAdapter(success=False)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
        )

        pipeline.run_tier3_background("bad\n", "test.ivy", track_state=False)
        assert pipeline._tier3.running is False
        assert pipeline._tier3.current_file is None
        # But failure is still recorded
        state = pipeline.get_pipeline_state()
        assert state["tier3Failed"] == 1

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


# ---------------------------------------------------------------------------
# Pipeline state tracking tests
# ---------------------------------------------------------------------------


class TestPipelineState:
    """Tests for get_pipeline_state() tier tracking."""

    def _make_pipeline(self, model=None, compiler=None):
        from ivy_lsp.adapters.null_adapter import (
            NullAstEnrichmentAdapter,
            NullCompilerAdapter,
            NullParserAdapter,
        )
        m = model or SemanticModel()
        return AnalysisPipeline(
            model=m,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler or NullCompilerAdapter(),
        ), m

    def test_initial_state_all_zeros(self):
        pipeline, _ = self._make_pipeline()
        state = pipeline.get_pipeline_state()
        assert state["tier1FileCount"] == 0
        assert state["tier2FileCount"] == 0
        assert state["tier3FileCount"] == 0
        assert state["tier3Running"] is False
        assert state["tier3Succeeded"] == 0
        assert state["tier3Failed"] == 0
        assert state["tier3CurrentFile"] is None
        assert state["tier3LastFile"] is None
        assert state["tier3LastCompletedAt"] is None
        assert state["semanticNodeCount"] == 0
        assert state["semanticEdgeCount"] == 0
        assert state["semanticModelReady"] is False

    def test_tier1_increments_file_count(self):
        pipeline, model = self._make_pipeline()
        pipeline.run_tier1("require x; # [rfc9000:4.1]\n", "a.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier1FileCount"] == 1
        assert state["semanticModelReady"] is True

    def test_tier2_increments_file_count(self):
        pipeline, _ = self._make_pipeline()
        pipeline.run_tier2("type cid\n", "b.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier2FileCount"] == 1

    def test_tier3_tracks_file_on_success(self):
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=True))
        pipeline.run_tier3_background("type cid\n", "c.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier3FileCount"] == 1
        assert state["tier3Running"] is False

    def test_tier3_failure_tracked_with_error(self):
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=False))
        pipeline.run_tier3_background("bad\n", "d.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier3FileCount"] == 1  # failures are now tracked
        assert state["tier3Succeeded"] == 0
        assert state["tier3Failed"] == 1
        assert state["tier3Running"] is False

    def test_tier3_success_and_failure_counts(self):
        compiler = StubCompilerAdapter(success=True)
        pipeline, _ = self._make_pipeline(compiler=compiler)
        pipeline.run_tier3_background("ok\n", "a.ivy")
        compiler._success = False
        pipeline.run_tier3_background("bad\n", "b.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier3FileCount"] == 2
        assert state["tier3Succeeded"] == 1
        assert state["tier3Failed"] == 1
        assert state["tier3LastFile"] == "b.ivy"
        assert state["tier3LastCompletedAt"] is not None

    def test_tier3_current_file_tracked(self):
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=True))
        # After synchronous completion, current_file should be None
        pipeline.run_tier3_background("ok\n", "test.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier3CurrentFile"] is None  # completed synchronously

    def test_get_tier3_file_results(self):
        compiler = StubCompilerAdapter(success=True)
        pipeline, _ = self._make_pipeline(compiler=compiler)
        pipeline.run_tier3_background("ok\n", "a.ivy")
        pipeline.run_tier3_background("ok\n", "b.ivy")
        results = pipeline.get_tier3_file_results()
        assert len(results) == 2
        assert results[0]["file"] == "b.ivy"  # newest first
        assert results[1]["file"] == "a.ivy"
        assert all(r["success"] for r in results)
        assert all(r["duration"] >= 0 for r in results)

    def test_multiple_files_counted_separately(self):
        pipeline, _ = self._make_pipeline()
        pipeline.run_tier1("require x; # [a]\n", "a.ivy")
        pipeline.run_tier1("require y; # [b]\n", "b.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier1FileCount"] == 2

    def test_same_file_counted_once(self):
        pipeline, _ = self._make_pipeline()
        pipeline.run_tier1("require x; # [a]\n", "a.ivy")
        pipeline.run_tier1("require y; # [b]\n", "a.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier1FileCount"] == 1

    def test_initial_state_has_tier3_pending_zero(self):
        pipeline, _ = self._make_pipeline()
        state = pipeline.get_pipeline_state()
        assert state["tier3Pending"] == 0

    def test_tier3_pending_zero_after_track_state_true(self):
        """track_state=True (default) should not touch _tier3_pending."""
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=True))
        pipeline.run_tier3_background("ok\n", "a.ivy", track_state=True)
        state = pipeline.get_pipeline_state()
        assert state["tier3Pending"] == 0

    def test_tier3_pending_zero_after_track_state_false_completes(self):
        """track_state=False increments then decrements on synchronous completion."""
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=True))
        pipeline.run_tier3_background("ok\n", "a.ivy", track_state=False)
        state = pipeline.get_pipeline_state()
        assert state["tier3Pending"] == 0

    def test_tier3_pending_zero_after_track_state_false_failure(self):
        """track_state=False decrements even on failure."""
        pipeline, _ = self._make_pipeline(compiler=StubCompilerAdapter(success=False))
        pipeline.run_tier3_background("bad\n", "a.ivy", track_state=False)
        state = pipeline.get_pipeline_state()
        assert state["tier3Pending"] == 0

    def test_tier3_pending_increments_before_callback(self):
        """Pending should be 1 while callback has not yet fired."""
        model = SemanticModel()
        captured_pending = []

        class DelayedCompiler:
            def compile_background(self, source, filename, callback):
                # Before calling back, check the pending count
                captured_pending.append(model)
                state = pipeline.get_pipeline_state()
                captured_pending.clear()
                captured_pending.append(state["tier3Pending"])
                # Now call back
                from ivy_lsp.adapters.protocols import CompileResult
                callback(CompileResult(success=True))

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=DelayedCompiler(),
        )

        pipeline.run_tier3_background("ok\n", "a.ivy", track_state=False)
        # During compile_background, before callback, pending was 1
        assert captured_pending[0] == 1
        # After completion, pending is back to 0
        assert pipeline.get_pipeline_state()["tier3Pending"] == 0


# ---------------------------------------------------------------------------
# Tier 2 parse_result reuse tests
# ---------------------------------------------------------------------------


class TestTier2ParseResultReuse:
    """Verify run_tier2() reuses a pre-parsed result when provided."""

    def test_pre_parsed_result_skips_internal_parse(self):
        """When a valid parse_result is provided, the parser should NOT be called."""
        model = SemanticModel()
        ta = TypeAnnotation(
            name="cid",
            qualified_name="quic.cid",
            sort_name="type",
            line=1,
            is_enum=False,
        )

        class TrackingParser:
            """Parser that tracks whether parse() was called."""

            def __init__(self):
                self.parse_called = False

            def parse(self, source, filename):
                self.parse_called = True
                return ParseResult(ast={"stub": True}, errors=[], success=True, filename=filename)

        tracking_parser = TrackingParser()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=tracking_parser,
            enrichment_adapter=StubEnrichmentAdapter([ta]),
            compiler_adapter=NullCompilerAdapter(),
        )

        pre_parsed = ParseResult(
            ast={"pre_parsed": True}, errors=[], success=True, filename="test.ivy"
        )
        pipeline.run_tier2("type cid\n", "test.ivy", parse_result=pre_parsed)

        assert not tracking_parser.parse_called
        type_nodes = model.get_nodes_by_type(TypeNode)
        assert len(type_nodes) == 1
        assert type_nodes[0].name == "cid"

    def test_none_parse_result_calls_internal_parse(self):
        """When parse_result is None (default), the parser should be called."""
        model = SemanticModel()

        class TrackingParser:
            def __init__(self):
                self.parse_called = False

            def parse(self, source, filename):
                self.parse_called = True
                return ParseResult(ast={"stub": True}, errors=[], success=True, filename=filename)

        tracking_parser = TrackingParser()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=tracking_parser,
            enrichment_adapter=StubEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        pipeline.run_tier2("type cid\n", "test.ivy")
        assert tracking_parser.parse_called

    def test_failed_parse_result_falls_back_to_internal_parse(self):
        """When parse_result has success=False, the parser should be called."""
        model = SemanticModel()

        class TrackingParser:
            def __init__(self):
                self.parse_called = False

            def parse(self, source, filename):
                self.parse_called = True
                return ParseResult(ast=None, errors=[], success=False, filename=filename)

        tracking_parser = TrackingParser()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=tracking_parser,
            enrichment_adapter=StubEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        bad_result = ParseResult(ast=None, errors=[], success=False, filename="test.ivy")
        pipeline.run_tier2("type cid\n", "test.ivy", parse_result=bad_result)
        assert tracking_parser.parse_called

    def test_pre_parsed_result_returned_by_run_tier2(self):
        """run_tier2() should return the pre-parsed result when provided."""
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=StubParserAdapter(success=True),
            enrichment_adapter=StubEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        pre_parsed = ParseResult(
            ast={"pre_parsed": True}, errors=[], success=True, filename="test.ivy"
        )
        returned = pipeline.run_tier2("type cid\n", "test.ivy", parse_result=pre_parsed)
        assert returned is pre_parsed


# ---------------------------------------------------------------------------
# T3 test-file redirection tests
# ---------------------------------------------------------------------------


class TestT3TestFileRedirection:
    """Verify T3 compilation redirects to the enclosing test file."""

    def test_save_trigger_redirects_t3_to_test_file(self, tmp_path):
        """When resolver returns a test file, T3 should compile that instead."""
        # Create a fake test file on disk so the pipeline can read it
        test_file = tmp_path / "test_quic.ivy"
        test_file.write_text("include quic_time\ninclude quic_stack\n")

        # Track what source/filepath T3 receives
        t3_calls = []

        class RecordingCompiler:
            def compile(self, source, filename):
                t3_calls.append((source, filename))
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        def resolver(filepath):
            if filepath == "module.ivy":
                return str(test_file)
            return None

        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=RecordingCompiler(),
            test_file_resolver=resolver,
        )

        pipeline.analyze("action foo = {}", "module.ivy", trigger="save")

        assert len(t3_calls) == 1
        compiled_source, compiled_filepath = t3_calls[0]
        assert compiled_filepath == str(test_file)
        assert compiled_source == "include quic_time\ninclude quic_stack\n"

    def test_save_trigger_no_redirect_when_resolver_returns_none(self):
        """When resolver returns None, T3 compiles the original file."""
        t3_calls = []

        class RecordingCompiler:
            def compile(self, source, filename):
                t3_calls.append((source, filename))
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=RecordingCompiler(),
            test_file_resolver=lambda fp: None,
        )

        pipeline.analyze("export action bar = {}", "test_file.ivy", trigger="save")

        assert len(t3_calls) == 1
        compiled_source, compiled_filepath = t3_calls[0]
        assert compiled_filepath == "test_file.ivy"
        assert compiled_source == "export action bar = {}"

    def test_save_trigger_no_redirect_without_resolver(self):
        """Without a resolver, T3 compiles the original file as before."""
        t3_calls = []

        class RecordingCompiler:
            def compile(self, source, filename):
                t3_calls.append((source, filename))
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=RecordingCompiler(),
        )

        pipeline.analyze("export action baz = {}", "module.ivy", trigger="save")

        assert len(t3_calls) == 1
        assert t3_calls[0] == ("export action baz = {}", "module.ivy")

    def test_change_trigger_does_not_invoke_resolver(self):
        """Change trigger (T1+T2 only) should not touch the resolver."""
        resolver_calls = []

        def resolver(filepath):
            resolver_calls.append(filepath)
            return "/some/test.ivy"

        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            test_file_resolver=resolver,
        )

        pipeline.analyze("action x = {}", "module.ivy", trigger="change")

        assert len(resolver_calls) == 0

    def test_t3_redirect_falls_back_on_unreadable_test_file(self):
        """If the test file can't be read, T3 falls back to original."""
        t3_calls = []

        class RecordingCompiler:
            def compile(self, source, filename):
                t3_calls.append((source, filename))
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=RecordingCompiler(),
            test_file_resolver=lambda fp: "/nonexistent/test.ivy",
        )

        pipeline.analyze("export action x = {}", "module.ivy", trigger="save")

        assert len(t3_calls) == 1
        assert t3_calls[0] == ("export action x = {}", "module.ivy")


# ---------------------------------------------------------------------------
# C1: Tier 3 double-decrement regression test
# ---------------------------------------------------------------------------


import threading

import pytest


# ---------------------------------------------------------------------------
# C2: Bulk compilation graph enrichment thread safety
# ---------------------------------------------------------------------------


class TestBulkCompilationThreadSafety:
    """C2: Verify concurrent graph enrichment doesn't corrupt data."""

    def test_concurrent_add_action(self):
        """Multiple threads adding actions concurrently should not lose data."""
        from ivy_lsp.analysis.requirement_graph import ActionNode, RequirementGraph

        graph = RequirementGraph()
        errors = []

        def add_actions(prefix, count):
            try:
                for i in range(count):
                    node = ActionNode(
                        id=f"{prefix}_{i}",
                        name=f"action_{prefix}_{i}",
                        qualified_name=f"mod.action_{prefix}_{i}",
                        file=f"/test/{prefix}.ivy",
                        line=i,
                    )
                    graph.add_action(node)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_actions, args=(f"t{i}", 50))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent enrichment errors: {errors}"
        assert len(graph.actions) == 200  # 4 threads * 50 actions

    def test_concurrent_semantic_model_update(self):
        """Multiple threads updating SemanticModel concurrently should not lose data."""
        model = SemanticModel()
        errors = []

        def add_nodes(prefix, count):
            try:
                for i in range(count):
                    from ivy_lsp.semantic.nodes import RfcAnnotation

                    node = RfcAnnotation(
                        id=f"{prefix}:{i}",
                        file=f"/test/{prefix}.ivy",
                        line=i,
                        tags=[f"tag_{prefix}_{i}"],
                    )
                    model.add_node(node)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_nodes, args=(f"t{i}", 50))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent model update errors: {errors}"
        assert model.node_count() == 200  # 4 threads * 50 nodes


# ---------------------------------------------------------------------------
# C1: Tier 3 double-decrement regression test
# ---------------------------------------------------------------------------


class TestTier3DoubleDecrement:
    """C1: Verify _tier3_pending is decremented exactly once on sync error."""

    def test_sync_fallback_no_double_decrement(self):
        """When _on_result raises internally, pending counter should
        decrement exactly once, not twice."""
        model = SemanticModel()

        class SyncOnlyCompiler:
            """Compiler without compile_background; triggers sync fallback."""

            def compile(self, source, filepath):
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=SyncOnlyCompiler(),
        )

        # Patch update_file to raise inside _on_result
        def raising_update(*args, **kwargs):
            raise RuntimeError("Simulated update_file failure")

        model.update_file = raising_update

        # Pre-set pending to 1 to detect double-decrement
        # (if double-decrement happens, it goes from 2 -> 0 instead of 2 -> 1)
        with pipeline._state_lock:
            pipeline._tier3.pending = 1

        with pytest.raises(RuntimeError, match="Simulated"):
            pipeline.run_tier3_background("source", "/test.ivy", track_state=False)

        with pipeline._state_lock:
            assert pipeline._tier3.pending == 1, (
                f"Expected 1 pending (pre-set=1 + increment=2 - single decrement=1), "
                f"got {pipeline._tier3.pending} (double-decrement bug)"
            )

    def test_sync_fallback_track_state_true_no_double_cleanup(self):
        """When track_state=True and _on_result raises, running flag should
        be cleaned up exactly once."""
        model = SemanticModel()

        class SyncOnlyCompiler:
            def compile(self, source, filepath):
                from ivy_lsp.adapters.protocols import CompileResult

                return CompileResult(success=True, errors=[])

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=SyncOnlyCompiler(),
        )

        def raising_update(*args, **kwargs):
            raise RuntimeError("Simulated update_file failure")

        model.update_file = raising_update

        with pytest.raises(RuntimeError, match="Simulated"):
            pipeline.run_tier3_background("source", "/test.ivy", track_state=True)

        # After _on_result's finally, running should be False
        with pipeline._state_lock:
            assert pipeline._tier3.running is False
            assert pipeline._tier3.current_file is None


# ---------------------------------------------------------------------------
# Unified compilation tests (RequirementGraph enrichment + bulk T3)
# ---------------------------------------------------------------------------


class _FakeCompiledModuleIR:
    """Minimal stand-in for CompiledModuleIR used in enrichment tests."""

    def __init__(self, source_file="test.ivy", success=True, actions=None):
        self.source_file = source_file
        self.success = success
        self.actions = actions or {}
        self.sorts = {}
        self.symbols = {}
        self.labeled_axioms = []
        self.labeled_conjectures = []
        self.requirements = []
        self.errors = []
        self.mixins = {}
        self.isolates = {}
        self.compile_duration = 0.1


class _FakeCompilerManager:
    """CompilerManager stub that invokes callbacks synchronously."""

    def __init__(self, ir=None):
        self._ir = ir or _FakeCompiledModuleIR()
        self._cache = {}

    def compile_async(self, source, filepath, callback):
        # Use pre-populated cache entry if available, else create a fake IR
        if filepath in self._cache:
            ir = self._cache[filepath]
        else:
            ir = _FakeCompiledModuleIR(source_file=filepath, success=self._ir.success)
            self._cache[filepath] = ir
        callback(ir)

    def get_cached(self, filepath):
        return self._cache.get(filepath)

    def get_stats(self):
        return {
            "cachedFiles": len(self._cache),
            "activeProcesses": 0,
            "maxConcurrent": 1,
        }


class _FakeRequirementGraph:
    """Minimal requirement graph to verify enrichment calls."""

    def __init__(self):
        self.actions = {}
        self.enrich_calls = []

    def add_action(self, action):
        self.actions[action.id] = action
        self.enrich_calls.append(action.id)

    def add_action_if_absent(self, action):
        if action.id not in self.actions:
            self.add_action(action)


class TestRequirementGraphEnrichmentInT3:
    """Verify that T3 _on_result enriches RequirementGraph when provided."""

    def test_t3_enriches_requirement_graph_on_success(self):
        """When requirement_graph is set, T3 should call enrich_requirement_graph."""
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()
        fake_graph = _FakeRequirementGraph()

        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
            compiler_manager=fake_mgr,
            requirement_graph=fake_graph,
        )

        # Pre-populate the cache so _on_result finds the IR
        from ivy_lsp.compilation.ir import CompiledModuleIR, ActionIR

        ir = CompiledModuleIR(
            source_file="test.ivy",
            success=True,
            actions={
                "quic.send": ActionIR(
                    name="quic.send",
                    formal_params=("src",),
                    formal_returns=(),
                ),
            },
        )
        fake_mgr._cache["test.ivy"] = ir

        pipeline.run_tier3_background("type cid\n", "test.ivy")

        assert "quic.send" in fake_graph.actions

    def test_t3_does_not_enrich_graph_when_not_provided(self):
        """When requirement_graph is None, T3 should not attempt graph enrichment."""
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()

        compiler = StubCompilerAdapter(success=True)
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=compiler,
            compiler_manager=fake_mgr,
            requirement_graph=None,
        )

        # Should not raise even though requirement_graph is None
        pipeline.run_tier3_background("type cid\n", "test.ivy")
        state = pipeline.get_pipeline_state()
        assert state["tier3FileCount"] == 1


class TestBulkTier3:
    """Verify run_bulk_tier3 state tracking and enrichment."""

    def test_bulk_tier3_updates_state(self, tmp_path):
        """run_bulk_tier3 should update _bulk_compile_* state fields."""
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=fake_mgr,
        )

        # Create test files
        f1 = tmp_path / "test1.ivy"
        f2 = tmp_path / "test2.ivy"
        f1.write_text("type cid\n")
        f2.write_text("type pkt_num\n")

        pipeline.run_bulk_tier3([str(f1), str(f2)])

        state = pipeline.get_pipeline_state()
        assert state["bulkCompileTotal"] == 2
        assert state["bulkCompileCompleted"] == 2
        assert state["bulkCompileRunning"] is False

    def test_bulk_tier3_skips_when_no_compiler_manager(self):
        """Without a CompilerManager, run_bulk_tier3 should be a no-op."""
        model = SemanticModel()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=None,
        )

        pipeline.run_bulk_tier3(["nonexistent.ivy"])

        state = pipeline.get_pipeline_state()
        assert state["bulkCompileRunning"] is False
        assert state["bulkCompileTotal"] == 0

    def test_bulk_tier3_skips_when_already_running(self, tmp_path):
        """If already running, run_bulk_tier3 should return immediately."""
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=fake_mgr,
        )

        # Manually set running flag
        with pipeline._state_lock:
            pipeline._bulk_compile.running = True

        f1 = tmp_path / "test.ivy"
        f1.write_text("type cid\n")

        pipeline.run_bulk_tier3([str(f1)])

        # Should still show the pre-set state (not overwritten)
        state = pipeline.get_pipeline_state()
        assert state["bulkCompileRunning"] is True
        assert state["bulkCompileTotal"] == 0  # was not overwritten

        # Cleanup
        with pipeline._state_lock:
            pipeline._bulk_compile.running = False

    def test_bulk_tier3_calls_notification_callback(self, tmp_path):
        """Notification callback should be invoked at least once."""
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()
        notifications = []

        def _on_notify(completed, total, filepath, success):
            notifications.append((completed, total, filepath, success))

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=fake_mgr,
            notification_callback=_on_notify,
        )

        f1 = tmp_path / "test.ivy"
        f1.write_text("type cid\n")

        pipeline.run_bulk_tier3([str(f1)])

        # At least the final notification should have fired
        assert len(notifications) >= 1
        last = notifications[-1]
        assert last[0] == 1  # completed
        assert last[1] == 1  # total


class TestGetPipelineStateCompilation:
    """Verify get_pipeline_state includes compilation fields."""

    def test_pipeline_state_includes_bulk_compile_fields(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )

        state = pipeline.get_pipeline_state()
        assert "bulkCompileRunning" in state
        assert "bulkCompileTotal" in state
        assert "bulkCompileCompleted" in state
        assert state["bulkCompileRunning"] is False
        assert state["bulkCompileTotal"] == 0
        assert state["bulkCompileCompleted"] == 0

    def test_pipeline_state_includes_compiler_manager_stats(self):
        model = SemanticModel()
        fake_mgr = _FakeCompilerManager()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=fake_mgr,
        )

        state = pipeline.get_pipeline_state()
        assert "cachedFiles" in state
        assert "activeProcesses" in state
        assert "maxConcurrent" in state
        assert state["maxConcurrent"] == 1


# ---------------------------------------------------------------------------
# Tier 1 -> Tier 2 annotation reuse tests
# ---------------------------------------------------------------------------


class TestTier1ReturnAnnotations:
    """run_tier1 should return parsed annotations for reuse by tier2."""

    def test_tier1_returns_annotations_list(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )
        source = "require x > 0; # [rfc9000:4.1]\naction foo"
        result = pipeline.run_tier1(source, "test.ivy")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].tags == ["rfc9000:4.1"]


class TestTier2ReusesAnnotations:
    """run_tier2 should accept rfc_annotations kwarg and skip re-parsing."""

    def test_tier2_skips_reparse_when_annotations_provided(self):
        model = SemanticModel()
        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
        )
        source = "require x > 0; # [rfc9000:4.1]"
        pre_annotations = [
            RfcAnnotation(
                id="test.ivy:0:0",
                file="test.ivy",
                line=0,
                tags=["rfc9000:4.1"],
            )
        ]

        with mock.patch(
            "ivy_lsp.semantic.analysis_pipeline.parse_file_rfc_annotations"
        ) as mock_parse:
            pipeline.run_tier2(
                source, "test.ivy", rfc_annotations=pre_annotations
            )
            mock_parse.assert_not_called()

        nodes = model.get_nodes_in_file("test.ivy")
        rfc_nodes = [n for n in nodes if isinstance(n, RfcAnnotation)]
        assert len(rfc_nodes) == 1
        assert rfc_nodes[0].tags == ["rfc9000:4.1"]


# ---------------------------------------------------------------------------
# Bulk T3 submitted_count race condition tests
# ---------------------------------------------------------------------------


class TestBulkTier3SubmittedCount:
    """Verify submitted_count tracks correctly for is_final."""

    def test_skipped_file_does_not_cause_premature_final(self, tmp_path):
        """When a file is unreadable, bulk compile must still finish remaining files."""
        model = SemanticModel()
        mock_compiler = mock.MagicMock()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=mock_compiler,
        )

        good_file = tmp_path / "good.ivy"
        good_file.write_text("# good file")
        bad_file = tmp_path / "bad.ivy"
        # Don't create bad_file -- OSError when opened

        callbacks = []

        def fake_compile_async(source, filepath, callback):
            callbacks.append(callback)

        mock_compiler.compile_async.side_effect = fake_compile_async

        pipeline.run_bulk_tier3(
            [str(bad_file), str(good_file)],
            progress_callback=None,
        )

        assert len(callbacks) == 1, "Only the readable file should be submitted"
        assert pipeline._bulk_compile.running is True, "Should still be running"

        mock_ir = mock.MagicMock()
        mock_ir.success = True
        callbacks[0](mock_ir)

        assert pipeline._bulk_compile.running is False, (
            "Should be done after all submitted files complete"
        )

    def test_submitted_count_incremented_before_async_call(self):
        """submitted_count must be >= completed_count when callback fires synchronously."""
        model = SemanticModel()
        mock_compiler = mock.MagicMock()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=mock_compiler,
        )

        def fire_immediately(source, filepath, callback):
            mock_ir = mock.MagicMock()
            mock_ir.success = True
            callback(mock_ir)

        mock_compiler.compile_async.side_effect = fire_immediately

        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ivy", delete=False
        ) as f:
            f.write("# test file")
            test_file = f.name

        try:
            pipeline.run_bulk_tier3(
                [test_file],
                progress_callback=None,
            )
            assert pipeline._bulk_compile.running is False
            assert pipeline._bulk_compile.completed == 1
        finally:
            os.unlink(test_file)


# ---------------------------------------------------------------------------
# Stale-generation discard tests (M9 fix coverage)
# ---------------------------------------------------------------------------


class TestStaleGenerationDiscard:
    """M9: Per-file generation counter discards stale T3 results."""

    def test_tier3_discards_stale_result_after_tier1_rerun(self):
        """If run_tier1 fires between T3 submit and callback, result is discarded."""
        model = SemanticModel()
        mock_compiler_adapter = mock.MagicMock()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=mock_compiler_adapter,
        )

        filepath = "stale_test.ivy"
        source_v1 = "# version 1\nrequire x > 0; # [rfc9000:4.1]"

        # T1 sets generation to 1
        pipeline.run_tier1(source_v1, filepath)
        assert pipeline._file_generation[filepath] == 1

        # Submit T3 -- capture the callback passed to compile_background
        captured_callback = []

        def capture_compile_bg(source, fpath, callback):
            captured_callback.append(callback)

        mock_compiler_adapter.compile_background.side_effect = capture_compile_bg

        pipeline.run_tier3_background(source_v1, filepath)
        assert len(captured_callback) == 1

        # Simulate file change: T1 runs again (gen becomes 2)
        source_v2 = "# version 2\nrequire y > 0; # [rfc9000:5.2]"
        pipeline.run_tier1(source_v2, filepath)
        assert pipeline._file_generation[filepath] == 2

        # T3 callback fires with stale gen=1 result
        from ivy_lsp.adapters.protocols import CompileResult

        stale_result = CompileResult(success=True, errors=[])
        captured_callback[0](stale_result)

        # Stale result should NOT be stored
        assert filepath not in pipeline._tier3_results

    def test_bulk_tier3_discards_stale_result(self, tmp_path):
        """Bulk T3 also respects generation counter."""
        model = SemanticModel()
        mock_compiler = mock.MagicMock()

        pipeline = AnalysisPipeline(
            model=model,
            parser_adapter=NullParserAdapter(),
            enrichment_adapter=NullAstEnrichmentAdapter(),
            compiler_adapter=NullCompilerAdapter(),
            compiler_manager=mock_compiler,
        )

        test_file = tmp_path / "bulk_stale.ivy"
        test_file.write_text("# original content")

        # T1 sets gen=1
        pipeline.run_tier1(test_file.read_text(), str(test_file))

        # Capture bulk callback
        callbacks = []

        def capture(source, fpath, cb):
            callbacks.append((fpath, cb))

        mock_compiler.compile_async.side_effect = capture

        pipeline.run_bulk_tier3([str(test_file)])
        assert len(callbacks) == 1

        # File changes -> gen becomes 2
        pipeline.run_tier1("# changed content", str(test_file))
        assert pipeline._file_generation[str(test_file)] == 2

        # Bulk callback fires with stale gen=1
        mock_ir = mock.MagicMock()
        mock_ir.success = True
        callbacks[0][1](mock_ir)

        # Completion counted (for progress) but model NOT enriched
        assert pipeline._bulk_compile.completed == 1
        # Stale result not stored
        assert str(test_file) not in pipeline._tier3_results
