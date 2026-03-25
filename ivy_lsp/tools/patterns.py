"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.patterns."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.patterns")
sys.modules[__name__] = _real
