"""Backward-compat shim — delegates to ivy_lsp.core.analysis.formula_analyzer."""

import importlib
import sys

_real = importlib.import_module("ivy_lsp.core.analysis.formula_analyzer")
sys.modules[__name__] = _real

# Preserve old logger name for tests that use caplog with it.
import logging

_real.logger = logging.getLogger("ivy_lsp.analysis.formula_analyzer")
