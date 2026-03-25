"""Backward-compat shim — delegates to ivy_lsp.lsp.ui.monitoring."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.ui.monitoring")
sys.modules[__name__] = _real
