"""Tests for LSP navigation fixes (C6, C7, H4, H5, H6)."""

import re
import sys
from pathlib import Path

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))


class TestDefinitionInclude:
    """H5: Include lines should be detected."""

    def test_include_line_detected(self):
        from ivy_lsp.features.definition import _INCLUDE_RE

        m = _INCLUDE_RE.match("include collections")
        assert m is not None
        assert m.group(1) == "collections"

    def test_include_with_indent(self):
        from ivy_lsp.features.definition import _INCLUDE_RE

        m = _INCLUDE_RE.match("    include order")
        assert m is not None
        assert m.group(1) == "order"

    def test_non_include_not_matched(self):
        from ivy_lsp.features.definition import _INCLUDE_RE

        m = _INCLUDE_RE.match("action send_pkt")
        assert m is None


class TestDefinitionSelfDeclaration:
    """H6: Declaration lines should be detected."""

    def test_declaration_keyword_detected(self):
        from ivy_lsp.features.definition import _DECL_RE

        lines = [
            ("relation stream_seen(C:cid, S:stream_id)", "stream_seen"),
            ("action send_pkt(dst:ip.endpoint)", "send_pkt"),
            ("type cid", "cid"),
            ("function get_val(x:t) : t", "get_val"),
            ("individual my_var : nat", "my_var"),
            ("module quic_frame", "quic_frame"),
            ("object quic_stack", "quic_stack"),
            ("isolate quic_server_test", "quic_server_test"),
        ]
        for line, expected_name in lines:
            m = _DECL_RE.match(line)
            assert m is not None, f"Failed to match: {line}"
            assert m.group(1) == expected_name

    def test_non_declaration_not_matched(self):
        from ivy_lsp.features.definition import _DECL_RE

        m = _DECL_RE.match("require x > 0")
        assert m is None
