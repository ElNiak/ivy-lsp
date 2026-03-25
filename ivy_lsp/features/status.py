"""Backward-compat shim — delegates to ivy_lsp.lsp.ui.status."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.ui.status")
sys.modules[__name__] = _real
