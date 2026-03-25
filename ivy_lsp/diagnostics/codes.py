"""Backward-compat shim — delegates to ivy_lsp.core.diagnostics.codes."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.diagnostics.codes")
sys.modules[__name__] = _real
