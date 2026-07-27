"""Parser state isolation for safe sequential Ivy parsing."""

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ivy_lsp.infra.config import get_config

logger = logging.getLogger(__name__)

# Serializes access to Ivy's module-level globals across threads.
# Without this, a background CompilerSession can clobber state mid-parse.
_ivy_state_lock = threading.Lock()

_DEFAULT_LOCK_TIMEOUT = get_config().lock_timeout


@dataclass
class ParseResult:
    """Result of parsing an Ivy source file."""

    ast: Optional[Any] = None
    errors: List[Any] = field(default_factory=list)
    success: bool = False
    filename: Optional[str] = None


class ParserSession:
    """Context manager that isolates Ivy parser global state.

    Saves and restores all mutable globals across ivy_parser, ivy_utils,
    and ivy_ast modules to allow safe sequential parsing without
    state leakage between files.

    Acquires ``_ivy_state_lock`` on entry so that only one thread can
    touch the shared Ivy globals at a time.

    Args:
        timeout: Seconds to wait for the lock.  ``None`` uses the default
            from ``IVY_LSP_LOCK_TIMEOUT`` (30 s).
    """

    def __init__(self, timeout: Optional[float] = None) -> None:
        """Store the lock acquisition timeout."""
        self._timeout = timeout if timeout is not None else _DEFAULT_LOCK_TIMEOUT

    def __enter__(self):
        """Acquire the parser lock and save all Ivy parser globals."""
        self._lock_acquired = _ivy_state_lock.acquire(timeout=self._timeout)
        if not self._lock_acquired:
            raise TimeoutError(
                f"Failed to acquire Ivy parser state lock within "
                f"{self._timeout}s; another parse may be stuck"
            )

        import ivy.ivy_ast as ia
        import ivy.ivy_parser as ip
        import ivy.ivy_utils as iu

        # Cache module refs for safe restore in __exit__
        self._ip = ip
        self._iu = iu
        self._ia = ia

        # Save all 12 globals
        self._saved = {
            "ip.error_list": ip.error_list,
            "ip.stack": ip.stack,
            "ip.special_attribute": ip.special_attribute,
            "ip.parent_object": ip.parent_object,
            "ip.global_attribute": ip.global_attribute,
            "ip.common_attribute": ip.common_attribute,
            "ip.importer": getattr(ip, "importer", None),
            "iu.filename": iu.filename,
            "iu.ivy_language_version": iu.ivy_language_version,
            "ia.lf_counter": ia.lf_counter,
            "ia.reference_lineno": ia.reference_lineno,
            "ia.always_clone_with_fresh_id": ia.always_clone_with_fresh_id,
        }

        # Reset to clean defaults
        ip.error_list = []
        ip.stack = []
        ip.special_attribute = None
        ip.parent_object = None
        ip.global_attribute = None
        ip.common_attribute = None
        ip.importer = None
        iu.filename = None
        iu.ivy_language_version = "1.7"
        ia.lf_counter = 0
        ia.reference_lineno = None
        ia.always_clone_with_fresh_id = False

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore saved Ivy parser globals and release the parser lock."""
        try:
            import ivy.ivy_ast as ia
            import ivy.ivy_parser as ip
            import ivy.ivy_utils as iu

            ip.error_list = self._saved["ip.error_list"]
            ip.stack = self._saved["ip.stack"]
            ip.special_attribute = self._saved["ip.special_attribute"]
            ip.parent_object = self._saved["ip.parent_object"]
            ip.global_attribute = self._saved["ip.global_attribute"]
            ip.common_attribute = self._saved["ip.common_attribute"]
            ip.importer = self._saved["ip.importer"]
            iu.filename = self._saved["iu.filename"]
            iu.ivy_language_version = self._saved["iu.ivy_language_version"]
            ia.lf_counter = self._saved["ia.lf_counter"]
            ia.reference_lineno = self._saved["ia.reference_lineno"]
            ia.always_clone_with_fresh_id = self._saved["ia.always_clone_with_fresh_id"]
        except Exception:
            logger.error(
                "CRITICAL: Failed to restore Ivy parser global state. "
                "Subsequent parses may be corrupted.",
                exc_info=True,
            )
        finally:
            if self._lock_acquired:
                _ivy_state_lock.release()
        return False  # don't suppress exceptions


class IvyParserWrapper:
    """Safe wrapper around ivy_parser.parse() with state isolation.

    Args:
        resolve_callback: Optional callback matching the signature
            ``(include_name: str, from_file: str) -> Optional[str]``.
            When set, the parser delegates ``include`` resolution to this
            callback before falling back to the built-in same-dir / stdlib
            search.  Typically wired to :meth:`IncludeResolver.resolve`.
    """

    def __init__(
        self,
        resolve_callback: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> None:
        """Store the optional include-resolution callback."""
        self._resolve_callback = resolve_callback

    def parse(
        self,
        source: str,
        filename: str = "<string>",
        timeout: Optional[float] = None,
    ) -> ParseResult:
        """Parse Ivy source with full global state isolation.

        Args:
            source: Ivy source code to parse.
            filename: Logical filename for error messages.
            timeout: Seconds to wait for the parser lock.  ``None`` uses
                the default.  Use a short value (e.g. 0.5) for UI features
                that can fall back to cached data.

        Raises:
            TimeoutError: If the lock cannot be acquired within *timeout*.

        Never raises for parse errors — captures those into ParseResult.
        """
        try:
            import ivy.ivy_parser as ip
            import ivy.ivy_utils as iu
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError(
                f"Z3 is required but not available: {exc}. "
                "Install via 'pip install z3-solver'."
            ) from exc

        with ParserSession(timeout=timeout):
            iu.filename = filename

            def _lsp_importer(name: str):
                """Resolve and parse an included module."""
                fname = name + ".ivy"
                current_file = iu.filename or filename
                candidate = None

                # Try resolve callback first (covers all 4 levels:
                # same-dir, staging, workspace root, stdlib)
                if self._resolve_callback is not None:
                    candidate = self._resolve_callback(name, current_file)

                # Fallback: original 2-level search (same dir + stdlib)
                if candidate is None:
                    from_dir = os.path.dirname(os.path.abspath(current_file))
                    candidate = os.path.join(from_dir, fname)
                    if not os.path.isfile(candidate):
                        try:
                            std_dir = iu.get_std_include_dir()
                            candidate = os.path.join(std_dir, fname)
                        except Exception:
                            candidate = None

                if candidate is None or not os.path.isfile(candidate):
                    raise iu.IvyError(
                        None,
                        "module {} not found".format(fname),
                    )
                with open(candidate) as f:
                    content = f.read()
                with iu.SourceFile(candidate):
                    return ip.parse(content, nested=True)

            ip.importer = _lsp_importer

            # Suppress Redefining errors during parse.  Circular
            # includes (root file not in Ivy's include guard set)
            # cause harmless symbol redefinitions that would otherwise
            # abort the parse.  We patch report_error to skip them so
            # the AST builds successfully.
            from ivy.ivy_parser import Redefining

            _orig_report_error = ip.report_error

            def _filtered_report_error(error):
                if isinstance(error, Redefining):
                    return
                _orig_report_error(error)

            ip.report_error = _filtered_report_error

            try:
                ast = ip.parse(source)
                return ParseResult(ast=ast, errors=[], success=True, filename=filename)
            except iu.ErrorList as e:
                from ivy_lsp.infra.utils.ivy_output import format_ivy_errors

                logger.debug(
                    "Parse errors for %s (%d error(s)): %s",
                    filename,
                    len(e.errors),
                    format_ivy_errors(list(e.errors)),
                )
                return ParseResult(
                    ast=None,
                    errors=list(e.errors),
                    success=False,
                    filename=filename,
                )
            except iu.IvyError as e:
                logger.debug("Parse error for %s: %s", filename, e)
                return ParseResult(
                    ast=None,
                    errors=[e],
                    success=False,
                    filename=filename,
                )
            except Exception as e:
                logger.warning(
                    "Unexpected parse error for %s: %s", filename, e, exc_info=True
                )
                return ParseResult(
                    ast=None,
                    errors=[e],
                    success=False,
                    filename=filename,
                )
            finally:
                ip.report_error = _orig_report_error
