"""Backward-compat shim — delegates to ivy_lsp.core.compilation.ir."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.compilation.ir")
sys.modules[__name__] = _real
