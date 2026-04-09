"""Tests for precomputed extraction data in build_semantic_model."""

import pytest

from ivy_lsp.core.semantic.model_builder import PrecomputedFileData


@pytest.mark.unit
class TestPrecomputedFileData:
    def test_construction(self):
        pfd = PrecomputedFileData(
            symbols=[
                {
                    "name": "foo",
                    "kind": 12,
                    "range": [0, 0, 10, 0],
                    "children": [],
                    "detail": None,
                    "file_path": "f.ivy",
                    "synthetic": False,
                }
            ],
            includes=["bar"],
            tier_used=1,
        )
        assert pfd.tier_used == 1
        assert len(pfd.symbols) == 1
        assert pfd.includes == ["bar"]

    def test_empty(self):
        pfd = PrecomputedFileData(symbols=[], includes=[], tier_used=3)
        assert pfd.symbols == []
        assert pfd.includes == []
