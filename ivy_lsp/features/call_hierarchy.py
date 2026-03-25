"""Backward-compat shim — delegates to ivy_lsp.lsp.navigation.call_hierarchy."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.navigation.call_hierarchy")
sys.modules[__name__] = _real
