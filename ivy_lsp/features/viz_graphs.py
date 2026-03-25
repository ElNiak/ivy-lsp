"""Backward-compat shim — delegates to ivy_lsp.lsp.viz_graphs."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.viz_graphs")
sys.modules[__name__] = _real
