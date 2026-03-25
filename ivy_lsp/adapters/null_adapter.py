"""Backward-compat shim — delegates to ivy_lsp.core.adapters.null_adapter."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.adapters.null_adapter")
sys.modules[__name__] = _real
