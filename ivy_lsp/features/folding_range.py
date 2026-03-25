"""Backward-compat shim — delegates to ivy_lsp.lsp.ui.folding_range."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.ui.folding_range")
sys.modules[__name__] = _real
