"""Backward-compat shim — delegates to ivy_lsp.lsp.ui.code_lens."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.ui.code_lens")
sys.modules[__name__] = _real
