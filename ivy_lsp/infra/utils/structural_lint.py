"""Backward-compat shim — delegates to ivy_lsp.core.structural_lint."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.structural_lint")
sys.modules[__name__] = _real
