"""Protocol interfaces for the Ivy LSP analysis pipeline.

Defines runtime-checkable Protocol classes that isolate ivy internal
imports behind clean interfaces.  Adapters implement these Protocols;
consumers (SemanticModel, AnalysisPipeline, MCP tools) depend only on
the Protocols, never on ivy internals directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, runtime_checkable

from ivy_lsp.parsing.parser_session import ParseResult

if TYPE_CHECKING:
    from ivy_lsp.semantic.snapshots import ModuleSnapshot, SignatureSnapshot


# ---------------------------------------------------------------------------
# Data classes returned by adapters
# ---------------------------------------------------------------------------


@dataclass
class TypeAnnotation:
    """Type information extracted from an AST declaration."""

    name: str
    qualified_name: str
    sort_name: Optional[str] = None
    arity: int = 0
    params: List[str] = field(default_factory=list)
    return_sort: Optional[str] = None
    is_enum: bool = False
    variants: List[str] = field(default_factory=list)
    file: Optional[str] = None
    line: int = 0


@dataclass
class CompileError:
    """A single error from the Ivy compiler."""

    message: str
    file: Optional[str] = None
    line: int = 0
    col: int = 0


@dataclass
class CompileResult:
    """Result of compiling an Ivy source file through the full compiler."""

    success: bool = False
    errors: List[CompileError] = field(default_factory=list)
    module_snapshot: Optional["ModuleSnapshot"] = None
    signature_snapshot: Optional["SignatureSnapshot"] = None


# ---------------------------------------------------------------------------
# Protocol definitions
# ---------------------------------------------------------------------------


@runtime_checkable
class IParserAdapter(Protocol):
    """Parses Ivy source text into an AST."""

    def parse(self, source: str, filename: str) -> ParseResult: ...


@runtime_checkable
class IAstEnrichmentAdapter(Protocol):
    """Extracts type annotations from a parsed AST."""

    def extract_type_info(
        self, ast: Any, filename: str, source: str
    ) -> List[TypeAnnotation]: ...


@runtime_checkable
class ICompilerAdapter(Protocol):
    """Compiles Ivy source through the full compiler pipeline."""

    def compile(self, source: str, filename: str) -> CompileResult: ...


@runtime_checkable
class ISemanticModelQuery(Protocol):
    """Future query facade -- not yet implemented."""

    def get_symbol_info(self, name: str) -> Optional[Dict[str, Any]]: ...

    def get_cross_references(self, node_id: str) -> Dict[str, Any]: ...

    def get_rfc_annotations(self, file: str) -> List[Dict[str, Any]]: ...

    def get_coverage_stats(self) -> Dict[str, Any]: ...

    def get_impact_analysis(self, symbol: str) -> Dict[str, Any]: ...

    def query_graph(self, query: Dict[str, Any]) -> List[Dict[str, Any]]: ...
