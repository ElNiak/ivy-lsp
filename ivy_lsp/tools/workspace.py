"""Backward-compat shim — real module lives at ivy_lsp.mcp.tools.workspace."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.tools.workspace")
sys.modules[__name__] = _real
