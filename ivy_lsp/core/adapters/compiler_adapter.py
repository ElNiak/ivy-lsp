"""Compiler adapter for Tier 3 full-compiler analysis.

Wraps ``ivy_compiler.ivy_from_string()`` with global state isolation
composing a ParserSession to also save/restore compiler and
module globals.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ivy_lsp.core.adapters.protocols import CompileError, CompileResult
from ivy_lsp.core.semantic.snapshots import (
    ModuleSnapshot,
    SignatureSnapshot,
    SortInfo,
    SymbolInfo,
)

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ivy-compiler")
atexit.register(_executor.shutdown, wait=False)


class CompilerSession:
    """Context manager extending ParserSession with compiler/module globals.

    Saves and restores ``ivy_module``, ``ivy_logic``, and ``ivy_compiler``
    globals in addition to the parser globals already handled by
    :class:`~ivy_lsp.parsing.parser_session.ParserSession`.

    Args:
        timeout: Seconds to wait for the parser lock.  Passed through to
            :class:`ParserSession`.
    """

    def __init__(self, timeout: Optional[float] = None) -> None:
        """Store the lock timeout for use when entering the session."""
        self._timeout = timeout

    def __enter__(self) -> CompilerSession:
        """Enter the compiler session, saving and resetting compiler globals."""
        from ivy_lsp.core.parsing.parser_session import ParserSession

        self._parser_session = ParserSession(timeout=self._timeout)
        self._parser_session.__enter__()

        try:
            import ivy.ivy_logic as il
            import ivy.ivy_module as im

            self._compiler_saved = {
                "im.module": getattr(im, "module", None),
                "il.sig": getattr(il, "sig", None),
            }
        except ImportError:
            self._compiler_saved = {}

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        """Exit the compiler session, restoring saved compiler globals."""
        # Restore compiler globals
        try:
            import ivy.ivy_logic as il
            import ivy.ivy_module as im

            if "im.module" in self._compiler_saved:
                im.module = self._compiler_saved["im.module"]
            if "il.sig" in self._compiler_saved:
                il.sig = self._compiler_saved["il.sig"]
        except ImportError:
            logger.warning(
                "Cannot restore compiler state: ivy modules no longer importable"
            )

        self._parser_session.__exit__(exc_type, exc_val, exc_tb)
        return False


class CompilerAdapter:
    """Wraps the Ivy compiler with state isolation.

    Implements :class:`~ivy_lsp.adapters.protocols.ICompilerAdapter`.
    When a :class:`CompilerManager` is provided, delegates compilation
    to a subprocess for full isolation.
    """

    def __init__(
        self,
        compiler_manager: Any = None,
        staging_dir: Optional[str] = None,
    ) -> None:
        """Initialize with optional compiler manager and staging dir."""
        self._manager = compiler_manager
        self._staging_dir = staging_dir

    def compile(self, source: str, filename: str) -> CompileResult:
        """Compile *source* through the full Ivy compiler pipeline.

        Returns a :class:`CompileResult` with module/signature snapshots.
        Never raises -- captures all errors.
        """
        if self._manager is not None:
            return self._compile_via_manager(source, filename)
        # Legacy in-process path
        try:
            import ivy.ivy_compiler as ic
        except ImportError:
            return CompileResult(
                success=False,
                errors=[CompileError(message="ivy.ivy_compiler not available")],
            )

        with CompilerSession():
            saved_cwd = os.getcwd()
            if self._staging_dir:
                os.chdir(self._staging_dir)
            try:
                import ivy.ivy_module as im
                import ivy.ivy_utils as iu

                iu.filename = filename
                with im.Module():
                    ic.ivy_from_string(source)

                    try:
                        module_snap = _extract_module_snapshot()
                        sig_snap = _extract_signature_snapshot()
                    except Exception:
                        logger.warning(
                            "Snapshot extraction failed after successful compile",
                            exc_info=True,
                        )
                        return CompileResult(
                            success=False,
                            errors=[
                                CompileError(
                                    message="Compilation succeeded but snapshot extraction failed",
                                    file=filename,
                                )
                            ],
                        )

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
            finally:
                os.chdir(saved_cwd)

    def _compile_via_manager(self, source: str, filename: str) -> CompileResult:
        """Compile via CompilerManager subprocess."""
        ir = self._manager.compile_sync(source, filename)
        if not ir.success:
            return CompileResult(
                success=False,
                errors=[CompileError(message=e, file=filename) for e in ir.errors],
            )
        try:
            module_snap = ModuleSnapshot.from_ir(ir)
            sig_snap = module_snap.signature
        except Exception:
            logger.warning("Snapshot conversion failed", exc_info=True)
            return CompileResult(
                success=False,
                errors=[
                    CompileError(
                        message="Compilation succeeded but snapshot extraction failed",
                        file=filename,
                    )
                ],
            )

        return CompileResult(
            success=True,
            module_snapshot=module_snap,
            signature_snapshot=sig_snap,
        )

    # Maximum time (seconds) before we warn about a long-running compilation.
    COMPILE_TIMEOUT = 120

    def compile_background(
        self, source: str, filename: str, callback: Optional[Callable] = None
    ) -> None:
        """Submit compilation to the background thread pool.

        A watchdog timer logs a warning if compilation exceeds
        ``COMPILE_TIMEOUT`` seconds, making long lock-hold times visible.
        """
        if self._manager is not None:
            self._compile_background_via_manager(source, filename, callback)
            return
        # Legacy in-process path
        timed_out = threading.Event()

        def _watchdog():
            if not timed_out.is_set():
                logger.warning(
                    "Background compilation for %s exceeded %ds timeout "
                    "(may be blocking other parsing operations)",
                    filename,
                    self.COMPILE_TIMEOUT,
                )

        timer = threading.Timer(self.COMPILE_TIMEOUT, _watchdog)
        timer.daemon = True
        timer.start()

        def _run() -> CompileResult:
            try:
                result = self.compile(source, filename)
            except Exception as exc:
                result = CompileResult(
                    success=False,
                    errors=[CompileError(message=str(exc), file=filename)],
                )
                if callback:
                    callback(result)
                raise
            else:
                if callback:
                    callback(result)
                return result
            finally:
                timed_out.set()
                timer.cancel()

        future = _executor.submit(_run)

        def _on_done(f):
            try:
                f.result()
            except Exception:
                logger.warning(
                    "Background compilation failed for %s",
                    filename,
                    exc_info=True,
                )

        future.add_done_callback(_on_done)

    def _compile_background_via_manager(
        self, source: str, filename: str, callback: Optional[Callable]
    ) -> None:
        """Background compilation via CompilerManager subprocess."""

        def _on_ir(ir: Any) -> None:
            if not ir.success:
                result = CompileResult(
                    success=False,
                    errors=[CompileError(message=e, file=filename) for e in ir.errors],
                )
            else:
                try:
                    module_snap = ModuleSnapshot.from_ir(ir)
                    sig_snap = module_snap.signature
                except Exception:
                    logger.warning(
                        "Snapshot conversion failed for successful compilation of %s",
                        filename,
                        exc_info=True,
                    )
                    result = CompileResult(
                        success=False,
                        errors=[
                            CompileError(
                                message="Compilation succeeded but snapshot extraction failed",
                                file=filename,
                            )
                        ],
                    )
                else:
                    result = CompileResult(
                        success=True,
                        module_snapshot=module_snap,
                        signature_snapshot=sig_snap,
                    )
            if callback:
                callback(result)

        self._manager.compile_async(source, filename, _on_ir)


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
        logger.debug("Module snapshot extraction unavailable", exc_info=True)
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
        logger.debug("Signature snapshot extraction unavailable", exc_info=True)
        return None
