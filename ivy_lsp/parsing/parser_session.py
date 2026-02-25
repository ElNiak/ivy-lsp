"""Parser state isolation for safe sequential Ivy parsing."""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


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
    """

    def __enter__(self):
        import ivy.ivy_ast as ia
        import ivy.ivy_parser as ip
        import ivy.ivy_utils as iu

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
        import ivy.ivy_ast as ia
        import ivy.ivy_parser as ip
        import ivy.ivy_utils as iu

        # Restore all globals unconditionally
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

        return False  # don't suppress exceptions


class IvyParserWrapper:
    """Safe wrapper around ivy_parser.parse() with state isolation."""

    def parse(self, source: str, filename: str = "<string>") -> ParseResult:
        """Parse Ivy source with full global state isolation.

        Never raises — captures all errors into ParseResult.
        """
        import ivy.ivy_parser as ip
        import ivy.ivy_utils as iu

        with ParserSession():
            iu.filename = filename

            def _lsp_importer(name: str):
                """Resolve and parse an included module."""
                fname = name + ".ivy"
                from_dir = os.path.dirname(os.path.abspath(filename))
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

            try:
                ast = ip.parse(source)
                return ParseResult(ast=ast, errors=[], success=True, filename=filename)
            except iu.ErrorList as e:
                logger.debug(
                    "Parse errors for %s (%d error(s)): %s",
                    filename,
                    len(e.errors),
                    "; ".join(str(err) for err in e.errors),
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
