"""Backward-compat shim — delegates to ivy_lsp.lsp.pygls_patches."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.pygls_patches")
sys.modules[__name__] = _real
