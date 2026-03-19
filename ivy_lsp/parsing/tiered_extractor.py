"""Tiered symbol extraction: parser -> lexer -> regex cascade.

Provides a unified ``TieredExtractor`` that attempts the richest available
parsing strategy first, falling back to cheaper alternatives when the
preferred tier fails or is unavailable.

Tier 1 (parser): ``IvyParserWrapper`` + ``ast_to_symbols`` — full AST,
    requires Z3 / ivy package.
Tier 2 (lexer): ``fallback_scan`` — PLY token-based symbol extraction,
    requires PLY but not Z3.
Tier 3 (regex): Lightweight regex patterns — always available, least
    accurate (can match inside comments / native blocks).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.symbols import IvySymbol, SymbolReference

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result data types
# ---------------------------------------------------------------------------


@dataclass
class TierError:
    """Record of a failed tier attempt."""

    tier: int
    error_type: str  # e.g. "ImportError", "ParseError", "LexerError"
    message: str
    timing_ms: float


@dataclass
class ExtractionResult:
    """Result of tiered symbol extraction from a single Ivy file."""

    symbols: List[IvySymbol] = field(default_factory=list)
    includes: List[str] = field(default_factory=list)
    references: List[SymbolReference] = field(default_factory=list)
    tier_used: int = 0
    timing_ms: float = 0.0
    errors: List[TierError] = field(default_factory=list)

    @property
    def symbol_count(self) -> int:
        """Total symbols including nested children."""

        def _count(syms: List[IvySymbol]) -> int:
            total = 0
            for s in syms:
                total += 1
                total += _count(s.children)
            return total

        return _count(self.symbols)


# ---------------------------------------------------------------------------
# Tier 3: Regex patterns (relocated from mcp_server.py)
# ---------------------------------------------------------------------------

INCLUDE_PATTERN = re.compile(r"^include\s+(\w+)", re.MULTILINE)

_TYPE_DECL_RE = re.compile(r"^\s*type\s+([\w.]+)(?:\s*=\s*\{([^}]+)\})?", re.MULTILINE)
_ACTION_DECL_RE = re.compile(
    r"^\s*action\s+([\w.]+)\s*(?:\(([^)]*)\))?(?:\s*returns\s*\(([^)]*)\))?",
    re.MULTILINE,
)
_RELATION_DECL_RE = re.compile(
    r"^\s*relation\s+([\w.]+)\s*(?:\(([^)]*)\))?", re.MULTILINE
)
_FUNCTION_DECL_RE = re.compile(
    r"^\s*function\s+([\w.]+)\s*(?:\(([^)]*)\))?(?:\s*:\s*(\w+))?",
    re.MULTILINE,
)
_INDIVIDUAL_DECL_RE = re.compile(r"^\s*individual\s+([\w.]+)\s*:\s*(\w+)", re.MULTILINE)
_OBJECT_DECL_RE = re.compile(
    r"^\s*(object|module|isolate)\s+([\w.]+)\s*(?:=\s*\{)?", re.MULTILINE
)

# ---------------------------------------------------------------------------
# Tier 2/3: Reference extraction patterns
# ---------------------------------------------------------------------------

_CALL_STMT_RE = re.compile(r"(?:call\s+)([\w.]+)\s*(?:\(|;|$)", re.MULTILINE)
_INSTANCE_RE = re.compile(r"^\s*instance\s+([\w.]+)\s*:\s*([\w.]+)", re.MULTILINE)
_MONITOR_RE = re.compile(r"^\s*(before|after|around)\s+([\w.]+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# TieredExtractor
# ---------------------------------------------------------------------------


class TieredExtractor:
    """Cascading symbol extraction: Parser -> Lexer -> Regex.

    Each tier is attempted in order.  On failure (ImportError, parse error,
    lexer error), the next tier is tried.  ImportErrors are cached per
    instance so repeated import attempts are avoided.

    Parameters
    ----------
    resolve_callback:
        Optional include-resolution callback for the parser tier.
        Signature: ``(include_name: str, from_file: str) -> Optional[str]``.
    parser_timeout:
        Seconds to wait for the parser lock (Tier 1).
    """

    def __init__(
        self,
        resolve_callback: Optional[Callable[[str, str], Optional[str]]] = None,
        parser_timeout: float = 5.0,
    ) -> None:
        """Initialize with optional resolve callback and parser timeout."""
        self._resolve_callback = resolve_callback
        self._parser_timeout = parser_timeout
        # Cached import-availability flags (None = not checked yet)
        self._parser_available: Optional[bool] = None
        self._lexer_available: Optional[bool] = None

    # -- Public API ---------------------------------------------------------

    def extract(
        self,
        source: str,
        filepath: str,
    ) -> ExtractionResult:
        """Extract symbols and includes from Ivy source using the best tier.

        Attempts Tier 1 (parser), then Tier 2 (lexer), then Tier 3 (regex).
        Returns an ``ExtractionResult`` with the symbols, includes, which
        tier succeeded, timing, and any errors from failed tiers.
        """
        if not source or not source.strip():
            return ExtractionResult(tier_used=0, timing_ms=0.0)

        errors: List[TierError] = []

        # -- Tier 1: Parser ------------------------------------------------
        if self._parser_available is not False:
            t0 = time.monotonic()
            try:
                symbols, includes, references = self._try_parser(source, filepath)
                elapsed = (time.monotonic() - t0) * 1000
                logger.debug(
                    "%s: tier=1 (parser) succeeded, %d symbols, %d includes (%.1fms)",
                    filepath,
                    _count_symbols(symbols),
                    len(includes),
                    elapsed,
                )
                result = ExtractionResult(
                    symbols=symbols,
                    includes=includes,
                    references=references,
                    tier_used=1,
                    timing_ms=elapsed,
                    errors=errors,
                )
                self._trace_result(filepath, result)
                return result
            except ImportError as exc:
                elapsed = (time.monotonic() - t0) * 1000
                self._parser_available = False
                err = TierError(1, "ImportError", str(exc), elapsed)
                errors.append(err)
                logger.debug(
                    "%s: tier=1 failed (ImportError: %s) (%.1fms) — parser unavailable, caching for this instance",
                    filepath,
                    exc,
                    elapsed,
                )
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                err = TierError(1, type(exc).__name__, str(exc), elapsed)
                errors.append(err)
                logger.debug(
                    "%s: tier=1 failed (%s: %s) (%.1fms)",
                    filepath,
                    type(exc).__name__,
                    exc,
                    elapsed,
                )

        # -- Tier 2: Lexer -------------------------------------------------
        if self._lexer_available is not False:
            t0 = time.monotonic()
            try:
                symbols, includes, references = self._try_lexer(source, filepath)
                elapsed = (time.monotonic() - t0) * 1000
                logger.debug(
                    "%s: tier=2 (lexer) succeeded, %d symbols, %d includes (%.1fms)",
                    filepath,
                    _count_symbols(symbols),
                    len(includes),
                    elapsed,
                )
                result = ExtractionResult(
                    symbols=symbols,
                    includes=includes,
                    references=references,
                    tier_used=2,
                    timing_ms=elapsed,
                    errors=errors,
                )
                self._trace_result(filepath, result)
                return result
            except ImportError as exc:
                elapsed = (time.monotonic() - t0) * 1000
                self._lexer_available = False
                err = TierError(2, "ImportError", str(exc), elapsed)
                errors.append(err)
                logger.debug(
                    "%s: tier=2 failed (ImportError: %s) (%.1fms) — lexer unavailable, caching for this instance",
                    filepath,
                    exc,
                    elapsed,
                )
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                err = TierError(2, type(exc).__name__, str(exc), elapsed)
                errors.append(err)
                logger.debug(
                    "%s: tier=2 failed (%s: %s) (%.1fms)",
                    filepath,
                    type(exc).__name__,
                    exc,
                    elapsed,
                )

        # -- Tier 3: Regex (always succeeds) --------------------------------
        t0 = time.monotonic()
        symbols, includes, references = self._try_regex(source, filepath)
        elapsed = (time.monotonic() - t0) * 1000
        logger.debug(
            "%s: tier=3 (regex) succeeded, %d symbols, %d includes (%.1fms)",
            filepath,
            _count_symbols(symbols),
            len(includes),
            elapsed,
        )
        result = ExtractionResult(
            symbols=symbols,
            includes=includes,
            references=references,
            tier_used=3,
            timing_ms=elapsed,
            errors=errors,
        )
        self._trace_result(filepath, result)
        return result

    @staticmethod
    def _trace_result(filepath: str, result: ExtractionResult) -> None:
        """Send extraction result to the debug tracer (no-op when disabled)."""
        from ivy_lsp.debug_trace import get_tracer

        tracer = get_tracer()
        if tracer is not None:
            tracer.trace_tier_selection(filepath, result)

    # -- Tier implementations -----------------------------------------------

    def _try_parser(
        self, source: str, filepath: str
    ) -> Tuple[List[IvySymbol], List[str], List[SymbolReference]]:
        """Tier 1: Full PLY parser via IvyParserWrapper + ast_to_symbols.

        Raises ImportError if ivy package is unavailable.
        Raises RuntimeError if parsing fails.
        """
        from ivy_lsp.parsing.ast_to_symbols import (
            ast_to_symbols,
            extract_references_from_ast,
        )
        from ivy_lsp.parsing.parser_session import IvyParserWrapper

        wrapper = IvyParserWrapper(resolve_callback=self._resolve_callback)
        result = wrapper.parse(source, filename=filepath, timeout=self._parser_timeout)

        if not result.success or result.ast is None:
            error_msgs = [str(e) for e in result.errors[:3]]
            raise RuntimeError(
                f"Parse failed with {len(result.errors)} error(s): "
                + "; ".join(error_msgs)
            )

        symbols = ast_to_symbols(result.ast, filepath, source)

        # Extract includes from AST declarations
        includes = _extract_includes_from_ast(result.ast)

        # Extract references from AST
        try:
            references = extract_references_from_ast(result.ast, filepath, source)
        except Exception:
            logger.debug(
                "AST reference extraction failed for %s", filepath, exc_info=True
            )
            references = []

        return symbols, includes, references

    def _try_lexer(
        self, source: str, filepath: str
    ) -> Tuple[List[IvySymbol], List[str], List[SymbolReference]]:
        """Tier 2: PLY lexer via fallback_scan.

        Raises ImportError if PLY is unavailable.
        Raises RuntimeError if the lexer fails entirely.
        """
        from ivy_lsp.parsing.fallback_scanner import fallback_scan

        symbols, error_info = fallback_scan(source, filename=filepath)

        if not symbols and error_info is not None:
            raise RuntimeError(
                f"Lexer error at line {error_info.get('line', '?')}: {error_info.get('message', 'unknown')}"
            )

        # Extract includes from the symbol list (fallback_scan emits
        # IvySymbol(kind=File, detail="include") for include directives)
        includes = [
            sym.name
            for sym in symbols
            if sym.kind == SymbolKind.File and sym.detail == "include"
        ]

        # Filter out the include symbols from the main symbol list
        # (they are metadata, not declarations)
        declaration_symbols = [
            sym
            for sym in symbols
            if not (sym.kind == SymbolKind.File and sym.detail == "include")
        ]

        # Extract references using same regex patterns as Tier 3
        references: List[SymbolReference] = []

        for m in _CALL_STMT_RE.finditer(source):
            target = m.group(1)
            call_line = source[: m.start()].count("\n")
            # Find enclosing action from extracted symbols
            best_name: Optional[str] = None
            best_line = -1
            for sym in declaration_symbols:
                if (
                    sym.kind == SymbolKind.Function
                    and sym.range[0] <= call_line
                    and sym.range[0] > best_line
                ):
                    best_name = sym.name
                    best_line = sym.range[0]
            if best_name:
                references.append(
                    SymbolReference(
                        source_name=best_name,
                        target_name=target,
                        kind="call",
                        line=call_line,
                        file_path=filepath,
                    )
                )

        for m in _INSTANCE_RE.finditer(source):
            references.append(
                SymbolReference(
                    source_name=m.group(1),
                    target_name=m.group(2),
                    kind="instance",
                    line=source[: m.start()].count("\n"),
                    file_path=filepath,
                )
            )

        for m in _MONITOR_RE.finditer(source):
            mk = m.group(1)
            an = m.group(2)
            references.append(
                SymbolReference(
                    source_name=f"{mk} {an}",
                    target_name=an,
                    kind="monitor",
                    line=source[: m.start()].count("\n"),
                    file_path=filepath,
                )
            )

        return declaration_symbols, includes, references

    def _try_regex(
        self, source: str, filepath: str
    ) -> Tuple[List[IvySymbol], List[str], List[SymbolReference]]:
        """Tier 3: Regex-based extraction (always succeeds)."""
        symbols: List[IvySymbol] = []
        lines = source.split("\n")

        def _line_range(offset: int) -> Tuple[int, int, int, int]:
            line_idx = source[:offset].count("\n")
            line_len = len(lines[line_idx]) if line_idx < len(lines) else 0
            return (line_idx, 0, line_idx, line_len)

        # Type declarations
        for m in _TYPE_DECL_RE.finditer(source):
            name = m.group(1)
            rng = _line_range(m.start())
            variants_raw = m.group(2)
            detail = None
            if variants_raw:
                detail = "enum: " + ", ".join(
                    v.strip() for v in variants_raw.split(",") if v.strip()
                )
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Class,
                    range=rng,
                    detail=detail or "type",
                    file_path=filepath,
                )
            )

        # Action declarations
        for m in _ACTION_DECL_RE.finditer(source):
            name = m.group(1)
            rng = _line_range(m.start())
            params_raw = m.group(2)
            ret_raw = m.group(3)
            detail_parts = []
            if params_raw:
                detail_parts.append(
                    "("
                    + ", ".join(p.strip() for p in params_raw.split(",") if p.strip())
                    + ")"
                )
            if ret_raw:
                detail_parts.append("returns (" + ret_raw.strip() + ")")
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Function,
                    range=rng,
                    detail=" ".join(detail_parts) if detail_parts else "action",
                    file_path=filepath,
                )
            )

        # Relation declarations
        for m in _RELATION_DECL_RE.finditer(source):
            name = m.group(1)
            rng = _line_range(m.start())
            params_raw = m.group(2)
            detail = "relation"
            if params_raw:
                detail = "(" + params_raw.strip() + ")"
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Function,
                    range=rng,
                    detail=detail,
                    file_path=filepath,
                )
            )

        # Function declarations
        for m in _FUNCTION_DECL_RE.finditer(source):
            name = m.group(1)
            rng = _line_range(m.start())
            ret_sort = m.group(3)
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Function,
                    range=rng,
                    detail=f": {ret_sort}" if ret_sort else "function",
                    file_path=filepath,
                )
            )

        # Individual declarations
        for m in _INDIVIDUAL_DECL_RE.finditer(source):
            name = m.group(1)
            rng = _line_range(m.start())
            sort_name = m.group(2)
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=SymbolKind.Variable,
                    range=rng,
                    detail=f": {sort_name}" if sort_name else "individual",
                    file_path=filepath,
                )
            )

        # Object/module/isolate declarations
        _KEYWORD_KIND_MAP = {
            "object": SymbolKind.Module,
            "module": SymbolKind.Module,
            "isolate": SymbolKind.Namespace,
        }
        for m in _OBJECT_DECL_RE.finditer(source):
            keyword = m.group(1)
            name = m.group(2)
            rng = _line_range(m.start())
            symbols.append(
                IvySymbol(
                    name=name,
                    kind=_KEYWORD_KIND_MAP.get(keyword, SymbolKind.Module),
                    range=rng,
                    detail=keyword,
                    file_path=filepath,
                )
            )

        # Includes
        includes = INCLUDE_PATTERN.findall(source)

        # --- Reference extraction ---
        references: List[SymbolReference] = []

        # Helper: find enclosing action for a given line
        def _find_enclosing_action(line_idx: int) -> Optional[str]:
            """Find which action's range contains this line."""
            best_name: Optional[str] = None
            best_line = -1
            for sym in symbols:
                if (
                    sym.kind == SymbolKind.Function
                    and sym.range[0] <= line_idx
                    and sym.range[0] > best_line
                ):
                    best_name = sym.name
                    best_line = sym.range[0]
            return best_name

        # CALLS: call X(...)
        for m in _CALL_STMT_RE.finditer(source):
            target = m.group(1)
            call_line = source[: m.start()].count("\n")
            enclosing = _find_enclosing_action(call_line)
            if enclosing:
                references.append(
                    SymbolReference(
                        source_name=enclosing,
                        target_name=target,
                        kind="call",
                        line=call_line,
                        file_path=filepath,
                    )
                )

        # USES: instance X : Y(...)
        for m in _INSTANCE_RE.finditer(source):
            inst_name = m.group(1)
            module_name = m.group(2)
            inst_line = source[: m.start()].count("\n")
            references.append(
                SymbolReference(
                    source_name=inst_name,
                    target_name=module_name,
                    kind="instance",
                    line=inst_line,
                    file_path=filepath,
                )
            )

        # MONITORS: before/after/around X
        for m in _MONITOR_RE.finditer(source):
            mixin_kind = m.group(1)  # "before", "after", "around"
            action_name = m.group(2)
            mon_line = source[: m.start()].count("\n")
            references.append(
                SymbolReference(
                    source_name=f"{mixin_kind} {action_name}",
                    target_name=action_name,
                    kind="monitor",
                    line=mon_line,
                    file_path=filepath,
                )
            )

        return symbols, includes, references


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_symbols(symbols: List[IvySymbol]) -> int:
    """Count symbols including nested children."""
    total = 0
    for s in symbols:
        total += 1
        total += _count_symbols(s.children)
    return total


def _extract_includes_from_ast(ast_obj: object) -> List[str]:
    """Extract include module names from a parsed Ivy AST.

    The Ivy parser merges included file declarations into the main AST,
    but we can detect include directives by looking for the ``included``
    set attribute, an ``includes`` list, or IncludeDecl nodes.
    """
    includes: List[str] = []

    # Method 1: Check for the ``included`` set (Ivy parser tracks this)
    included_set = getattr(ast_obj, "included", None)
    if included_set and isinstance(included_set, (set, frozenset)):
        return sorted(str(name) for name in included_set)

    # Method 2: Check for an explicit includes list on the AST
    raw_includes = getattr(ast_obj, "includes", None)
    if raw_includes:
        for inc in raw_includes:
            name = getattr(inc, "name", None) or getattr(inc, "relname", None)
            if name:
                includes.append(str(name))
        if includes:
            return includes

    # Method 3: Walk declarations looking for IncludeDecl
    try:
        import ivy.ivy_ast as ia

        for decl in getattr(ast_obj, "decls", []):
            if isinstance(decl, getattr(ia, "IncludeDecl", type(None))):
                name = getattr(decl, "name", None) or getattr(decl, "relname", None)
                if name:
                    includes.append(str(name))
    except ImportError:
        pass

    return includes
