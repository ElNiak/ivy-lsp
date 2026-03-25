"""Backward-compat shim — delegates to ivy_lsp.core.adapters.protocols."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.adapters.protocols")
sys.modules[__name__] = _real
