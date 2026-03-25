# tests/test_infra_skeleton.py
def test_infra_package_importable():
    import ivy_lsp.infra

    assert hasattr(ivy_lsp.infra, "__name__")
