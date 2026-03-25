"""Backward-compat shim — delegates to ivy_lsp.lsp.viz_coverage."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.viz_coverage")
sys.modules[__name__] = _real
