"""Tests for the shared Ivy PLY lexer tokenization module."""

import pytest

from ivy_lsp.parsing.token_stream import TokenStream, tokenize_ivy


class TestTokenizeIvy:
    """tokenize_ivy() returns a TokenStream with correct tokens."""

    def test_empty_source(self):
        stream = tokenize_ivy("", "test.ivy")
        assert isinstance(stream, TokenStream)
        assert stream.tokens == []
        assert stream.error_info is None
        assert stream.filename == "test.ivy"

    def test_simple_action_tokens(self):
        source = "action foo = {\n    require x > 0;\n}\n"
        stream = tokenize_ivy(source, "test.ivy")
        assert stream.error_info is None
        types = [t.type for t in stream.tokens]
        assert "ACTION" in types
        assert "PRESYMBOL" in types
        assert "LCB" in types
        assert "REQUIRE" in types
        assert "SEMI" in types
        assert "RCB" in types

    def test_comments_stripped(self):
        source = "# this is a comment\naction foo = {}\n"
        stream = tokenize_ivy(source, "test.ivy")
        types = [t.type for t in stream.tokens]
        assert all("COMMENT" not in t for t in types)
        assert "ACTION" in types

    def test_lines_populated(self):
        source = "action foo = {\n    require x > 0;\n}\n"
        stream = tokenize_ivy(source, "test.ivy")
        assert stream.lines == ["action foo = {", "    require x > 0;", "}", ""]

    def test_source_preserved(self):
        source = "type cid\n"
        stream = tokenize_ivy(source, "test.ivy")
        assert stream.source == source

    def test_token_lineno_is_1_based(self):
        source = "type cid\naction foo = {}\n"
        stream = tokenize_ivy(source, "test.ivy")
        first = stream.tokens[0]
        assert first.lineno >= 1

    def test_monitor_tokens_recognized(self):
        source = "before foo.step {\n    require x > 0;\n}\n"
        stream = tokenize_ivy(source, "test.ivy")
        types = [t.type for t in stream.tokens]
        assert "BEFORE" in types
        assert "REQUIRE" in types

    def test_native_block_is_single_token(self):
        source = 'action foo = { <<< std::cout << "hi"; >>> }\n'
        stream = tokenize_ivy(source, "test.ivy")
        types = [t.type for t in stream.tokens]
        assert "NATIVEQUOTE" in types


class TestTokenStreamDataclass:
    """TokenStream dataclass behavior."""

    def test_manual_construction(self):
        stream = TokenStream(tokens=[], source="", filename="f.ivy")
        assert stream.lines == [""]
        assert stream.error_info is None

    def test_lines_from_source(self):
        stream = TokenStream(tokens=[], source="a\nb\nc", filename="f.ivy")
        assert stream.lines == ["a", "b", "c"]

    def test_explicit_lines_not_overwritten(self):
        stream = TokenStream(tokens=[], source="a\nb", filename="f.ivy", lines=["x"])
        assert stream.lines == ["x"]
