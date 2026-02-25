"""Compiler adapter for Tier 3 full-compiler analysis.

Wraps ``ivy_compiler.ivy_from_string()`` with global state isolation
extending the ParserSession pattern to also save/restore compiler and
module globals.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ivy_lsp.adapters.protocols import CompileError, CompileResult
from ivy_lsp.semantic.snapshots import (
    ModuleSnapshot,
    SignatureSnapshot,
    SortInfo,
    SymbolInfo,
)

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ivy-compiler")


class CompilerSession:
    """Context manager extending ParserSession with compiler/module globals.

    Saves and restores ``ivy_module``, ``ivy_logic``, and ``ivy_compiler``
    globals in addition to the parser globals already handled by
    :class:`~ivy_lsp.parsing.parser_session.ParserSession`.
    """

    def __enter__(self) -> CompilerSession:
        from ivy_lsp.parsing.parser_session import ParserSession

        self._parser_session = ParserSession()
        self._parser_session.__enter__()

        try:
            import ivy.ivy_module as im
            import ivy.ivy_logic as il

            self._compiler_saved = {
                "im.module": getattr(im, "module", None),
                "il.sig": getattr(il, "sig", None),
            }
        except ImportError:
            self._compiler_saved = {}

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        # Restore compiler globals
        try:
            import ivy.ivy_module as im
            import ivy.ivy_logic as il

            if "im.module" in self._compiler_saved:
                im.module = self._compiler_saved["im.module"]
            if "il.sig" in self._compiler_saved:
                il.sig = self._compiler_saved["il.sig"]
        except ImportError:
            pass

        self._parser_session.__exit__(exc_type, exc_val, exc_tb)
        return False


class CompilerAdapter:
    """Wraps the Ivy compiler with state isolation.

    Implements :class:`~ivy_lsp.adapters.protocols.ICompilerAdapter`.
    """

    def compile(self, source: str, filename: str) -> CompileResult:
        """Compile *source* through the full Ivy compiler pipeline.

        Returns a :class:`CompileResult` with module/signature snapshots.
        Never raises -- captures all errors.
        """
        try:
            import ivy.ivy_compiler as ic
        except ImportError:
            return CompileResult(
                success=False,
                errors=[CompileError(message="ivy.ivy_compiler not available")],
            )

        with CompilerSession():
            try:
                import ivy.ivy_utils as iu

                iu.filename = filename
                ic.ivy_from_string(source)

                # Extract snapshots
                module_snap = _extract_module_snapshot()
                sig_snap = _extract_signature_snapshot()

                return CompileResult(
                    success=True,
                    module_snapshot=module_snap,
                    signature_snapshot=sig_snap,
                )
            except Exception as e:
                error = CompileError(
                    message=str(e),
                    file=filename,
                )
                return CompileResult(success=False, errors=[error])

    def compile_background(
        self, source: str, filename: str, callback: Optional[Callable] = None
    ) -> None:
        """Submit compilation to the background thread pool."""

        def _run() -> CompileResult:
            result = self.compile(source, filename)
            if callback:
                callback(result)
            return result

        _executor.submit(_run)


def _extract_module_snapshot() -> Optional[ModuleSnapshot]:
    """Extract a ModuleSnapshot from the current ivy_module state."""
    try:
        import ivy.ivy_module as im

        mod = getattr(im, "module", None)
        if mod is None:
            return None

        sig_snap = _extract_signature_snapshot()
        return ModuleSnapshot(
            signature=sig_snap,
            axioms=[str(a) for a in getattr(mod, "labeled_axioms", [])],
            conjectures=[str(c) for c in getattr(mod, "labeled_conjs", [])],
            isolates=list(getattr(mod, "isolates", {}).keys()),
            raw_module=mod,
        )
    except (ImportError, AttributeError):
        return None


def _extract_signature_snapshot() -> Optional[SignatureSnapshot]:
    """Extract a SignatureSnapshot from the current ivy_logic.sig state."""
    try:
        import ivy.ivy_logic as il

        sig = getattr(il, "sig", None)
        if sig is None:
            return None

        sorts = {}
        for name, sort in getattr(sig, "sorts", {}).items():
            arity = getattr(sort, "arity", 0)
            sorts[name] = SortInfo(name=name, arity=arity)

        symbols = {}
        for name, sym in getattr(sig, "symbols", {}).items():
            sort_str = str(getattr(sym, "sort", ""))
            symbols[name] = SymbolInfo(name=name, sort=sort_str)

        return SignatureSnapshot(
            sorts=sorts,
            symbols=symbols,
            actions=list(getattr(sig, "actions", {}).keys()),
            relations=[
                n for n, s in symbols.items() if getattr(s, "is_relation", False)
            ],
        )
    except (ImportError, AttributeError):
        return None
