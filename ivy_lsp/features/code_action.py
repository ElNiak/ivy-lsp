"""Backward-compat shim — delegates to ivy_lsp.lsp.code_action."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.code_action")
sys.modules[__name__] = _real
