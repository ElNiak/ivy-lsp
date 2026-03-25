"""Backward-compat shim — delegates to ivy_lsp.core.parsing.fallback_parser."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.parsing.fallback_parser")
sys.modules[__name__] = _real
