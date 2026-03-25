"""Backward-compat shim — delegates to ivy_lsp.core.verification."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.verification")
sys.modules[__name__] = _real
