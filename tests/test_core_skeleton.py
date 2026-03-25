def test_core_package_importable():
    import ivy_lsp.core

    assert hasattr(ivy_lsp.core, "__name__")
