"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.formatters.traceability."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.formatters.traceability")
sys.modules[__name__] = _real
