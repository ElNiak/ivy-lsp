"""Backward-compat shim — delegates to ivy_lsp.lsp.index_builder."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.index_builder")
sys.modules[__name__] = _real
