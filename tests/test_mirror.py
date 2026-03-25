"""Tests for Mirror, MirrorId, and MirrorRegistry."""

import os

import pytest

from ivy_lsp.core.analysis.mirror import MirrorId, MirrorRole


class TestMirrorId:
    def test_create_from_fields(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_str_representation(self):
        mid = MirrorId(protocol="quic", entry_stem="quic_server_test_stream")
        assert str(mid) == "quic::quic_server_test_stream"

    def test_from_test_file(self):
        mid = MirrorId.from_test_file(
            "/path/to/quic_server_test_stream.ivy", protocol="quic"
        )
        assert mid.protocol == "quic"
        assert mid.entry_stem == "quic_server_test_stream"

    def test_from_test_file_strips_ivy_extension(self):
        mid = MirrorId.from_test_file("/any/path/foo_test.ivy", protocol="minip")
        assert mid.entry_stem == "foo_test"

    def test_hashable_and_eq(self):
        a = MirrorId(protocol="quic", entry_stem="test_a")
        b = MirrorId(protocol="quic", entry_stem="test_a")
        c = MirrorId(protocol="quic", entry_stem="test_b")
        assert a == b
        assert a != c
        assert {a, b, c} == {a, c}

    def test_from_test_file_no_extension(self):
        mid = MirrorId.from_test_file("/path/to/test_file", protocol="quic")
        assert mid.entry_stem == "test_file"

    def test_frozen(self):
        mid = MirrorId(protocol="quic", entry_stem="test")
        with pytest.raises(AttributeError):
            mid.protocol = "other"


class TestMirrorRole:
    def test_values(self):
        assert MirrorRole.CLIENT.value == "client"
        assert MirrorRole.SERVER.value == "server"
        assert MirrorRole.MIM.value == "mim"
        assert MirrorRole.UNKNOWN.value == "unknown"

    def test_from_string(self):
        assert MirrorRole("client") == MirrorRole.CLIENT
