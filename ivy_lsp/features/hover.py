"""Backward-compat shim — delegates to ivy_lsp.lsp.navigation.hover."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.navigation.hover")
sys.modules[__name__] = _real
