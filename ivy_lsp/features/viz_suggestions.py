"""Backward-compat shim — delegates to ivy_lsp.lsp.viz_suggestions."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.viz_suggestions")
sys.modules[__name__] = _real
