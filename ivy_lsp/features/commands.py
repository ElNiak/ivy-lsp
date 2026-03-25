"""Backward-compat shim — delegates to ivy_lsp.lsp.commands."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.commands")
sys.modules[__name__] = _real
