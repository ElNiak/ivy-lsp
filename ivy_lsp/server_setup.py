"""Backward-compat shim — delegates to ivy_lsp.lsp.server_setup."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.server_setup")
sys.modules[__name__] = _real
