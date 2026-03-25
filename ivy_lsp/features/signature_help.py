"""Backward-compat shim — delegates to ivy_lsp.lsp.signature_help."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.signature_help")
sys.modules[__name__] = _real
