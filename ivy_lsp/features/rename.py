"""Backward-compat shim — delegates to ivy_lsp.lsp.rename."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.rename")
sys.modules[__name__] = _real
