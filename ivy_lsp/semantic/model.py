"""Backward-compat shim — delegates to ivy_lsp.core.semantic.model."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.semantic.model")
sys.modules[__name__] = _real
