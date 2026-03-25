"""Backward-compat shim — delegates to ivy_lsp.lsp.document_highlight."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.lsp.document_highlight")
sys.modules[__name__] = _real
