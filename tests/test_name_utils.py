"""Tests for ivy_lsp.infra.utils.name_utils."""

from ivy_lsp.infra.utils.name_utils import get_last_component


class TestGetLastComponent:
    def test_dotted_name(self):
        assert get_last_component("a.b.c") == "c"

    def test_single_name(self):
        assert get_last_component("foo") == "foo"

    def test_one_dot(self):
        assert get_last_component("frame.ack") == "ack"

    def test_empty_string(self):
        assert get_last_component("") == ""

    def test_trailing_dot(self):
        assert get_last_component("a.") == ""

    def test_only_dot(self):
        assert get_last_component(".") == ""
