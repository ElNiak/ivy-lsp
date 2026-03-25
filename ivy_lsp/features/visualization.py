"""Backward-compat shim — delegates to ivy_lsp.lsp.visualization."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.visualization")
sys.modules[__name__] = _real
