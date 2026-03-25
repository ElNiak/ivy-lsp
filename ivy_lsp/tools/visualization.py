"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.visualization."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.visualization")
sys.modules[__name__] = _real
