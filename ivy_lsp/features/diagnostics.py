"""Backward-compat shim — delegates to ivy_lsp.lsp.diagnostics.publisher."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.diagnostics.publisher")
sys.modules[__name__] = _real
