"""Backward-compat shim — delegates to ivy_lsp.core.parsing.symbols."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.parsing.symbols")
sys.modules[__name__] = _real
