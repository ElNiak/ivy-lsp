"""Backward-compat shim — delegates to ivy_lsp.core.diagnostics.rich_diagnostic."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.diagnostics.rich_diagnostic")
sys.modules[__name__] = _real
