"""Backward-compat shim — delegates to ivy_lsp.lsp.bulk_orchestrator."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.bulk_orchestrator")
sys.modules[__name__] = _real
