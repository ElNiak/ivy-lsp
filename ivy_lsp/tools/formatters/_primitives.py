"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.formatters._primitives."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.formatters._primitives")
sys.modules[__name__] = _real
