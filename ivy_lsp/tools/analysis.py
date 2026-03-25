"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.analysis."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.analysis")
sys.modules[__name__] = _real
