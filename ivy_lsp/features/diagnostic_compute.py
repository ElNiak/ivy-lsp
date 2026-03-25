"""Backward-compat shim — delegates to ivy_lsp.lsp.diagnostics.compute."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.diagnostics.compute")
sys.modules[__name__] = _real
