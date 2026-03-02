"""M7: All 6 unprotected LSP handlers must have try/except."""

import inspect

import pytest


@pytest.mark.parametrize(
    "module_path,handler_name",
    [
        ("ivy_lsp.features.hover", "hover"),
        ("ivy_lsp.features.definition", "definition"),
        ("ivy_lsp.features.signature_help", "signature_help"),
        ("ivy_lsp.features.folding_range", "folding_range"),
        ("ivy_lsp.features.document_symbols", "document_symbol"),
        ("ivy_lsp.features.code_action", "code_action"),
    ],
)
def test_handler_has_try_except(module_path, handler_name):
    """Each handler's register function must contain try/except."""
    import importlib

    mod = importlib.import_module(module_path)
    source = inspect.getsource(mod.register)
    assert "try:" in source and "except" in source, (
        f"{module_path}.register must wrap handler in try/except"
    )
