"""Backward-compat shim — delegates to ivy_lsp.lsp.navigation.definition."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.navigation.definition")
sys.modules[__name__] = _real
