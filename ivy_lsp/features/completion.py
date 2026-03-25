"""Backward-compat shim — delegates to ivy_lsp.lsp.completion."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.completion")
sys.modules[__name__] = _real
