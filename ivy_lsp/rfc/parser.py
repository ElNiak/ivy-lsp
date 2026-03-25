"""Backward-compat shim — delegates to ivy_lsp.core.rfc.parser."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.rfc.parser")
sys.modules[__name__] = _real
