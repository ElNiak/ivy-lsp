"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.formatters.primitives."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.formatters.primitives")
sys.modules[__name__] = _real
