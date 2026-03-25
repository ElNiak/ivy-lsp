"""Backward-compat shim — delegates to ivy_lsp.core.semantic.snapshots."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.semantic.snapshots")
sys.modules[__name__] = _real
