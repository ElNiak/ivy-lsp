"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.traceability_extraction."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.traceability_extraction")
sys.modules[__name__] = _real
