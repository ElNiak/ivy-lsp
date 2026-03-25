"""Backward-compat shim — delegates to ivy_lsp.lsp.commands_extended."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.commands_extended")
sys.modules[__name__] = _real
