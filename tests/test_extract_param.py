"""Tests for the _extract_param utility function."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.lsp.commands_helpers import _extract_param


class TestExtractParam:
    """Tests for _extract_param covering all input formats."""

    def test_flat_list(self):
        assert _extract_param(["quic.send"], "actionName") == "quic.send"

    def test_nested_list(self):
        assert (
            _extract_param([["quic.send", "file:///a.ivy"]], "actionName")
            == "quic.send"
        )

    def test_dict(self):
        assert _extract_param({"actionName": "quic.send"}, "actionName") == "quic.send"

    def test_dict_missing_key(self):
        assert _extract_param({"other": "val"}, "actionName") is None

    def test_object_with_arguments(self):
        obj = MagicMock()
        obj.arguments = ["quic.send"]
        assert _extract_param(obj, "actionName") == "quic.send"

    def test_object_with_nested_arguments(self):
        obj = MagicMock()
        obj.arguments = [["quic.send", "extra"]]
        assert _extract_param(obj, "actionName") == "quic.send"

    def test_none_returns_none(self):
        assert _extract_param(None, "actionName") is None

    def test_empty_list_returns_none(self):
        assert _extract_param([], "actionName") is None

    def test_empty_nested_list(self):
        assert _extract_param([[]], "actionName") is None

    def test_non_string_value_returns_none(self):
        """Non-string values should be rejected."""
        assert _extract_param([42], "key") is None

    def test_non_string_dict_value_returns_none(self):
        assert _extract_param({"key": 42}, "key") is None

    def test_non_string_nested_returns_none(self):
        assert _extract_param([[42, "extra"]], "key") is None
