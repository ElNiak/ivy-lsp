"""Backward-compat shim — delegates to ivy_lsp.lsp.workspace_symbols."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.workspace_symbols")
sys.modules[__name__] = _real
