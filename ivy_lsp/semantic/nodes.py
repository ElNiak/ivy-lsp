"""Backward-compat shim — delegates to ivy_lsp.core.semantic.nodes."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.semantic.nodes")
sys.modules[__name__] = _real
