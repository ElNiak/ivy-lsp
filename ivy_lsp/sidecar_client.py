"""Backward-compat shim — real module lives at ivy_lsp.mcp.client."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.mcp.client")
sys.modules[__name__] = _real
