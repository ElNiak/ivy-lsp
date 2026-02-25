"""Three-tier analysis pipeline orchestrator.

Tier 1 (syntactic, <50ms): structural checks + RFC annotation parsing
Tier 2 (AST-enriched, <200ms): parser + type info + requirement extraction
Tier 3 (compiler, background): full compiler analysis (placeholder)
"""

from __future__ import annotations

import logging
from typing import Any, List, Tuple

from ivy_lsp.adapters.protocols import (
    IAstEnrichmentAdapter,
    ICompilerAdapter,
    IParserAdapter,
)
from ivy_lsp.semantic.edges import SemanticEdgeType
from ivy_lsp.semantic.model import SemanticModel
from ivy_lsp.semantic.nodes import SymbolNode, TypeNode
from ivy_lsp.semantic.rfc_annotations import parse_file_rfc_annotations

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    """Orchestrates the three analysis tiers feeding into a SemanticModel."""

    def __init__(
        self,
        model: SemanticModel,
        parser_adapter: IParserAdapter,
        enrichment_adapter: IAstEnrichmentAdapter,
        compiler_adapter: ICompilerAdapter,
    ) -> None:
        self._model = model
        self._parser = parser_adapter
        self._enrichment = enrichment_adapter
        self._compiler = compiler_adapter

    # -- Tier 1 ----------------------------------------------------------------

    def run_tier1(self, source: str, filepath: str) -> None:
        """Immediate syntactic analysis (<50ms, no ivy dep).

        - Parse RFC annotations from comments
        - Feed into model.update_file at tier1
        """
        nodes: List[Any] = []
        edges: List[Tuple[str, SemanticEdgeType, str]] = []

        # RFC annotation parsing from comments
        annotations = parse_file_rfc_annotations(source, filepath)
        for ann in annotations:
            nodes.append(ann)

        self._model.update_file(filepath, nodes, edges, "tier1")
        logger.debug("Tier 1 complete for %s: %d nodes", filepath, len(nodes))

    # -- Tier 2 ----------------------------------------------------------------

    def run_tier2(self, source: str, filepath: str) -> None:
        """AST-enriched analysis (<200ms, ivy parser).

        - Parse with parser_adapter
        - Extract type info with enrichment_adapter
        - Build cross-reference edges (HAS_PARAM)
        - Re-parse RFC annotations to link with AST nodes
        - Feed into model.update_file at tier2
        """
        nodes: List[Any] = []
        edges: List[Tuple[str, SemanticEdgeType, str]] = []

        # Parse
        result = self._parser.parse(source, filepath)

        if result.success and result.ast is not None:
            # Extract type info
            type_annotations = self._enrichment.extract_type_info(
                result.ast, filepath, source
            )
            for ta in type_annotations:
                if ta.is_enum or ta.sort_name != "action":
                    node: Any = TypeNode(
                        id=f"{filepath}:{ta.line}:{ta.name}",
                        name=ta.name,
                        qualified_name=ta.qualified_name,
                        file=filepath,
                        line=ta.line,
                        sort_name=ta.sort_name,
                        is_enum=ta.is_enum,
                        variants=ta.variants,
                        tier="tier2",
                    )
                else:
                    node = SymbolNode(
                        id=f"{filepath}:{ta.line}:{ta.name}",
                        name=ta.name,
                        qualified_name=ta.qualified_name,
                        kind="action",
                        file=filepath,
                        line=ta.line,
                        sort_name=ta.sort_name,
                        arity=ta.arity,
                        params=ta.params,
                        return_sort=ta.return_sort,
                        tier="tier2",
                    )
                nodes.append(node)

                # Wire HAS_PARAM edges for actions with parameters
                if hasattr(node, "params") and node.params:
                    for param in node.params:
                        # param format: "name:sort"
                        if ":" in param:
                            sort_part = param.split(":", 1)[1]
                            edges.append(
                                (node.id, SemanticEdgeType.HAS_PARAM, sort_part)
                            )

        # Also re-parse RFC annotations at tier2 to link them with AST nodes
        annotations = parse_file_rfc_annotations(source, filepath)
        for ann in annotations:
            nodes.append(ann)

        self._model.update_file(filepath, nodes, edges, "tier2")
        logger.debug(
            "Tier 2 complete for %s: %d nodes, %d edges",
            filepath,
            len(nodes),
            len(edges),
        )

    # -- Tier 3 ----------------------------------------------------------------

    def run_tier3_background(self, source: str, filepath: str) -> None:
        """Schedule Tier 3 compiler analysis in background thread."""
        from ivy_lsp.adapters.protocols import CompileResult

        def _on_result(result: CompileResult) -> None:
            if not result.success:
                logger.debug(
                    "Tier 3 compilation failed for %s: %s",
                    filepath,
                    [e.message for e in result.errors],
                )
                return
            nodes: List[Any] = []
            edges: List[Tuple[str, SemanticEdgeType, str]] = []
            # Tier 3 nodes would come from compiler snapshots
            # For now, just mark the file as tier3-analyzed
            self._model.update_file(filepath, nodes, edges, "tier3")
            logger.debug("Tier 3 complete for %s", filepath)

        if hasattr(self._compiler, "compile_background"):
            self._compiler.compile_background(source, filepath, _on_result)
        else:
            # Synchronous fallback
            result = self._compiler.compile(source, filepath)
            _on_result(result)

    # -- Orchestration ---------------------------------------------------------

    def analyze(self, source: str, filepath: str, trigger: str = "change") -> None:
        """Run appropriate tiers based on trigger.

        trigger: "change" -> T1+T2, "save" -> T1+T2+T3, "command" -> T3 only
        """
        if trigger in ("change", "save"):
            self.run_tier1(source, filepath)
            self.run_tier2(source, filepath)
        if trigger in ("save", "command"):
            self.run_tier3_background(source, filepath)
