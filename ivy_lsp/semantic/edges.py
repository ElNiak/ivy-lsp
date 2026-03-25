"""Backward-compat shim — delegates to ivy_lsp.core.semantic.edges."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.semantic.edges")
sys.modules[__name__] = _real
