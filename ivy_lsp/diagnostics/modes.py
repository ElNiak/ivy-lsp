"""Backward-compat shim — delegates to ivy_lsp.core.diagnostics.modes."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.diagnostics.modes")
sys.modules[__name__] = _real
