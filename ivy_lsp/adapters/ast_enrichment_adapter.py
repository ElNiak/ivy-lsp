"""AST enrichment adapter for Tier 2 type information extraction.

Walks declarations in a parsed Ivy AST to extract type annotations:
sort names, enum variants, formal parameters with sorts, and return sorts.
Requires ``ivy`` to be installed; degrades gracefully if unavailable.
"""

from __future__ import annotations

import logging
from typing import Any, List

from ivy_lsp.adapters.protocols import TypeAnnotation

logger = logging.getLogger(__name__)


class AstEnrichmentAdapter:
    """Extracts type annotations from a parsed Ivy AST.

    Walks declarations to extract sort names, enum variants,
    formal parameters with sorts, and return sorts.

    Implements :class:`~ivy_lsp.adapters.protocols.IAstEnrichmentAdapter`.
    """

    def extract_type_info(
        self, ast: Any, filename: str, source: str
    ) -> List[TypeAnnotation]:
        """Walk *ast*.decls and return a :class:`TypeAnnotation` per declaration.

        Returns an empty list when *ast* is ``None`` or has no ``decls``.
        Individual declaration failures are logged at DEBUG and skipped.
        """
        if ast is None or not hasattr(ast, "decls"):
            return []

        annotations: List[TypeAnnotation] = []
        for decl in ast.decls:
            try:
                self._process_decl(decl, filename, annotations)
            except Exception:
                logger.warning(
                    "Failed to extract type info from %s",
                    type(decl).__name__,
                    exc_info=True,
                )
        return annotations

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _process_decl(
        self, decl: Any, filename: str, annotations: List[TypeAnnotation]
    ) -> None:
        """Dispatch *decl* to the appropriate extractor."""
        try:
            import ivy.ivy_ast as ia
        except ImportError:
            # ivy is unavailable -- nothing to extract.
            return

        if isinstance(decl, ia.TypeDecl):
            self._extract_type_decl(decl, filename, annotations)
            return

        if isinstance(decl, ia.ActionDecl):
            self._extract_action_decl(decl, filename, annotations)
            return

    # ------------------------------------------------------------------
    # TypeDecl extractor
    # ------------------------------------------------------------------

    def _extract_type_decl(
        self,
        decl: Any,
        filename: str,
        annotations: List[TypeAnnotation],
    ) -> None:
        """Extract sort name and optional enum variants from a TypeDecl."""
        defs = decl.defines()
        if not defs:
            return

        name = defs[0][0] if isinstance(defs[0], tuple) else str(defs[0])

        variants: List[str] = []
        is_enum = False
        sort_name = name

        # TypeDecl.args[0] may be a TypeDef whose second arg is an
        # EnumeratedSort listing variant names.
        if hasattr(decl, "args") and decl.args:
            sort_def = decl.args[0]
            if hasattr(sort_def, "extension") and sort_def.extension:
                is_enum = True
                for ext in sort_def.extension:
                    ext_name = getattr(ext, "rep", None) or str(ext)
                    variants.append(ext_name)

        line = _extract_line(decl)

        annotations.append(
            TypeAnnotation(
                name=name,
                qualified_name=name,
                sort_name=sort_name,
                is_enum=is_enum,
                variants=variants,
                file=filename,
                line=line,
            )
        )

    # ------------------------------------------------------------------
    # ActionDecl extractor
    # ------------------------------------------------------------------

    def _extract_action_decl(
        self,
        decl: Any,
        filename: str,
        annotations: List[TypeAnnotation],
    ) -> None:
        """Extract formal params with sorts and return sort from an ActionDecl."""
        defs = decl.defines()
        if not defs:
            return

        name = defs[0][0] if isinstance(defs[0], tuple) else str(defs[0])

        params: List[str] = []
        return_sort = None

        try:
            action_def = decl.args[0]

            # Formal parameters
            formal_params = getattr(action_def, "formal_params", [])
            for param in formal_params:
                p_name = (
                    getattr(param, "rep", None)
                    or getattr(param, "relname", None)
                    or str(param)
                )
                p_sort = getattr(param, "sort", None)
                if p_sort:
                    params.append(f"{p_name}:{p_sort}")
                else:
                    params.append(str(p_name))

            # Return sort (first formal return)
            formal_returns = getattr(action_def, "formal_returns", [])
            if formal_returns:
                ret = formal_returns[0]
                ret_sort = getattr(ret, "sort", None)
                if ret_sort:
                    return_sort = str(ret_sort)
        except (IndexError, AttributeError):
            pass

        line = _extract_line(decl)

        annotations.append(
            TypeAnnotation(
                name=name,
                qualified_name=name,
                sort_name="action",
                arity=len(params),
                params=params,
                return_sort=return_sort,
                file=filename,
                line=line,
            )
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _extract_line(decl: Any) -> int:
    """Return the 0-based line number for *decl*, or 0 if unavailable."""
    if hasattr(decl, "lineno") and hasattr(decl.lineno, "line"):
        return max(0, decl.lineno.line - 1)
    return 0
