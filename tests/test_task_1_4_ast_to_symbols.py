"""Tests for Task 1.4: AST-to-Symbol Converter."""

import pytest
from lsprotocol.types import SymbolKind

from ivy_lsp.parsing.ast_to_symbols import ast_to_symbols
from ivy_lsp.parsing.parser_session import IvyParserWrapper
from ivy_lsp.parsing.symbols import IvySymbol

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_and_convert(source: str, filename: str = "test.ivy"):
    """Parse source and convert AST to symbols."""
    wrapper = IvyParserWrapper()
    result = wrapper.parse(source, filename)
    assert result.success, f"Parse failed: {result.errors}"
    return ast_to_symbols(result.ast, filename, source)


def _find_symbol(symbols, name, kind=None):
    """Find a symbol by name (and optionally kind) in a flat or nested list."""
    for sym in symbols:
        if sym.name == name and (kind is None or sym.kind == kind):
            return sym
        # Also search children
        found = _find_symbol(sym.children, name, kind)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Test: Simple type declaration
# ---------------------------------------------------------------------------


class TestSimpleType:
    """Type declarations produce SymbolKind.Class."""

    def test_simple_type_produces_class_symbol(self):
        source = "#lang ivy1.7\n\ntype cid\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "cid")
        assert sym is not None, "Expected symbol 'cid'"
        assert sym.kind == SymbolKind.Class

    def test_simple_type_range_is_zero_based(self):
        source = "#lang ivy1.7\n\ntype cid\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "cid")
        assert sym is not None
        # 'type cid' is on line 3 (1-based), so line_idx=2 (0-based)
        assert sym.range[0] == 2, f"Expected start line 2, got {sym.range[0]}"

    def test_multiple_types(self):
        source = "#lang ivy1.7\n\ntype cid\ntype pkt_num\n"
        symbols = _parse_and_convert(source)
        assert _find_symbol(symbols, "cid") is not None
        assert _find_symbol(symbols, "pkt_num") is not None


# ---------------------------------------------------------------------------
# Test: Enum type
# ---------------------------------------------------------------------------


class TestEnumType:
    """Enum types produce Class with detail mentioning variants."""

    def test_enum_type_is_class(self):
        source = "#lang ivy1.7\n\ntype sk = {a, b}\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "sk")
        assert sym is not None
        assert sym.kind == SymbolKind.Class

    def test_enum_type_detail_mentions_variants(self):
        source = "#lang ivy1.7\n\ntype sk = {a, b}\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "sk")
        assert sym is not None
        assert sym.detail is not None
        assert "a" in sym.detail
        assert "b" in sym.detail


# ---------------------------------------------------------------------------
# Test: Object declaration
# ---------------------------------------------------------------------------


class TestObjectDecl:
    """Object declarations produce SymbolKind.Module."""

    def test_object_is_module(self):
        source = """\
#lang ivy1.7

object bit = {
    type this
    individual zero:bit
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "bit", SymbolKind.Module)
        assert sym is not None, "Expected Module symbol 'bit'"

    def test_object_has_children(self):
        source = """\
#lang ivy1.7

object bit = {
    type this
    individual zero:bit
    individual one:bit
}
"""
        symbols = _parse_and_convert(source)
        bit_sym = _find_symbol(symbols, "bit", SymbolKind.Module)
        assert bit_sym is not None
        # Children should include 'this' type and constants 'zero', 'one'
        child_names = [c.name for c in bit_sym.children]
        assert "zero" in child_names, f"Expected 'zero' in children, got {child_names}"
        assert "one" in child_names, f"Expected 'one' in children, got {child_names}"


# ---------------------------------------------------------------------------
# Test: Action declaration
# ---------------------------------------------------------------------------


class TestActionDecl:
    """Action declarations produce SymbolKind.Method (stateful procedures)."""

    def test_action_is_method(self):
        source = """\
#lang ivy1.7

type cid

action send(src:cid, dst:cid) returns (result:cid) = {
    result := src
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "send")
        assert sym is not None
        assert sym.kind == SymbolKind.Method

    def test_action_detail_contains_params(self):
        source = """\
#lang ivy1.7

type cid

action send(src:cid, dst:cid) returns (result:cid) = {
    result := src
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "send")
        assert sym is not None
        assert sym.detail is not None
        assert "src" in sym.detail
        assert "dst" in sym.detail

    def test_action_detail_contains_returns(self):
        source = """\
#lang ivy1.7

type cid

action send(src:cid, dst:cid) returns (result:cid) = {
    result := src
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "send")
        assert sym is not None
        assert sym.detail is not None
        assert "result" in sym.detail


# ---------------------------------------------------------------------------
# Test: Relation (parses as ConstantDecl with bool sort and args)
# ---------------------------------------------------------------------------


class TestRelation:
    """Relations produce SymbolKind.Function (they have parameters)."""

    def test_relation_is_function(self):
        source = """\
#lang ivy1.7

type cid

relation connected(X:cid, Y:cid)
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "connected")
        assert sym is not None
        assert sym.kind == SymbolKind.Function


# ---------------------------------------------------------------------------
# Test: Function keyword (pure function, not action)
# ---------------------------------------------------------------------------


class TestFunctionKeyword:
    """function keyword produces pure Function, distinct from action Method."""

    def test_function_is_function_not_method(self):
        source = """\
#lang ivy1.7

type t
function f(X:t) : t
"""
        symbols = _parse_and_convert(source)
        # function keyword parses as ConstantDecl or DerivedDecl, not ActionDecl
        # It should be Variable (ConstantDecl) or Function (if has args), NOT Method
        sym = _find_symbol(symbols, "f")
        assert sym is not None
        assert (
            sym.kind != SymbolKind.Method
        ), f"Pure function should NOT be Method (that's for actions), got {sym.kind}"


# ---------------------------------------------------------------------------
# Test: Alias declaration
# ---------------------------------------------------------------------------


class TestAliasDecl:
    """Alias declarations produce SymbolKind.Variable."""

    def test_alias_is_variable(self):
        source = """\
#lang ivy1.7

type cid
alias aid = cid
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "aid")
        assert sym is not None
        assert sym.kind == SymbolKind.Variable


# ---------------------------------------------------------------------------
# Test: Isolate declaration
# ---------------------------------------------------------------------------


class TestIsolateDecl:
    """Isolate declarations produce SymbolKind.Namespace."""

    def test_isolate_is_namespace(self):
        source = """\
#lang ivy1.7

type node

object protocol = {
    action step(n:node)
}

isolate iso_protocol = protocol
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "iso_protocol")
        assert sym is not None
        assert sym.kind == SymbolKind.Namespace


# ---------------------------------------------------------------------------
# Test: Property and Axiom
# ---------------------------------------------------------------------------


class TestPropertyAxiom:
    """PropertyDecl and AxiomDecl produce SymbolKind.Property."""

    def test_axiom_is_property(self):
        source = """\
#lang ivy1.7

type t
relation r(X:t, Y:t)

axiom [symmetry] r(X,Y) -> r(Y,X)
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "symmetry")
        assert sym is not None
        assert sym.kind == SymbolKind.Property

    def test_property_decl_is_property(self):
        source = """\
#lang ivy1.7

type t
relation r(X:t, Y:t)

property [reflexivity] r(X,X)
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "reflexivity")
        assert sym is not None
        assert sym.kind == SymbolKind.Property


# ---------------------------------------------------------------------------
# Test: Nested objects (hierarchy reconstruction from flat list)
# ---------------------------------------------------------------------------


class TestNestedObjects:
    """Flat dot-prefixed names are reconstructed into parent/child hierarchy."""

    def test_nested_object_becomes_child(self):
        source = """\
#lang ivy1.7

object frame = {
    object ack = {
        type this
    }
    type this
}
"""
        symbols = _parse_and_convert(source)
        frame_sym = _find_symbol(symbols, "frame", SymbolKind.Module)
        assert frame_sym is not None
        # 'ack' should be a child of 'frame', not at top level
        ack_child = None
        for child in frame_sym.children:
            if child.name == "ack" and child.kind == SymbolKind.Module:
                ack_child = child
                break
        assert ack_child is not None, (
            f"Expected 'ack' as child of 'frame', "
            f"got children: {[c.name for c in frame_sym.children]}"
        )

    def test_nested_object_not_at_top_level(self):
        source = """\
#lang ivy1.7

object frame = {
    object ack = {
        type this
    }
    type this
}
"""
        symbols = _parse_and_convert(source)
        # Top-level should have 'frame' but NOT 'frame.ack' or standalone 'ack'
        top_names = [s.name for s in symbols]
        assert "frame.ack" not in top_names
        assert "frame" in top_names

    def test_deeply_nested_constants(self):
        source = """\
#lang ivy1.7

object frame = {
    object ack = {
        type this
        individual largest_acked:frame.ack
    }
    type this
}
"""
        symbols = _parse_and_convert(source)
        frame_sym = _find_symbol(symbols, "frame", SymbolKind.Module)
        assert frame_sym is not None
        ack_child = _find_symbol(frame_sym.children, "ack", SymbolKind.Module)
        assert ack_child is not None
        # 'largest_acked' should be nested under ack
        child_names = [c.name for c in ack_child.children]
        assert (
            "largest_acked" in child_names
        ), f"Expected 'largest_acked' in ack children, got {child_names}"

    def test_four_level_nesting(self):
        """Four levels of nested objects are correctly reconstructed."""
        source = """\
#lang ivy1.7

object a = {
    object b = {
        object c = {
            type this
        }
    }
}
"""
        symbols = _parse_and_convert(source)
        a_sym = _find_symbol(symbols, "a", SymbolKind.Module)
        assert a_sym is not None
        b_sym = _find_symbol(a_sym.children, "b", SymbolKind.Module)
        assert (
            b_sym is not None
        ), f"Expected 'b' as child of 'a', got: {[c.name for c in a_sym.children]}"
        c_sym = _find_symbol(b_sym.children, "c", SymbolKind.Module)
        assert (
            c_sym is not None
        ), f"Expected 'c' as child of 'b', got: {[c.name for c in b_sym.children]}"
        # 'type this' should be child of c
        this_sym = _find_symbol(c_sym.children, "this", SymbolKind.Class)
        assert (
            this_sym is not None or len(c_sym.children) >= 1
        ), f"Expected 'this' in c's children, got: {[c.name for c in c_sym.children]}"

    def test_duplicate_name_object_plus_type(self):
        """Object and inner 'type this' sharing name 'bit': type nests under object.

        The parser expands ``type this`` inside ``object bit`` to a TypeDecl
        named ``"bit"``, so both the ObjectDecl and the TypeDecl share the
        same name.  The Module (object) should win as the root symbol and
        the Class (type) should be nested as its child.
        """
        source = """\
#lang ivy1.7

object bit = {
    type this
    individual zero:bit
}
"""
        symbols = _parse_and_convert(source)
        # 'bit' Module should be at root
        bit_module = _find_symbol(symbols, "bit", SymbolKind.Module)
        assert bit_module is not None, "Expected Module 'bit' at root"
        # The type 'bit' (from 'type this') should be a Class child of the Module
        type_child = None
        for c in bit_module.children:
            if c.kind == SymbolKind.Class:
                type_child = c
                break
        assert type_child is not None, (
            f"Expected a Class child (from 'type this') under 'bit' Module, got: "
            f"{[(c.name, c.kind) for c in bit_module.children]}"
        )


# ---------------------------------------------------------------------------
# Test: File path propagation
# ---------------------------------------------------------------------------


class TestFilePathPropagation:
    """All symbols should carry the filename."""

    def test_all_symbols_have_file_path(self):
        source = """\
#lang ivy1.7

type cid
type pkt_num
alias aid = cid
"""
        symbols = _parse_and_convert(source, filename="myfile.ivy")
        for sym in symbols:
            assert (
                sym.file_path == "myfile.ivy"
            ), f"Symbol '{sym.name}' has file_path={sym.file_path!r}"

    def test_children_have_file_path(self):
        source = """\
#lang ivy1.7

object bit = {
    type this
    individual zero:bit
}
"""
        symbols = _parse_and_convert(source, filename="objects.ivy")
        bit_sym = _find_symbol(symbols, "bit", SymbolKind.Module)
        assert bit_sym is not None
        for child in bit_sym.children:
            assert (
                child.file_path == "objects.ivy"
            ), f"Child '{child.name}' has file_path={child.file_path!r}"


# ---------------------------------------------------------------------------
# Test: 0-based range values
# ---------------------------------------------------------------------------


class TestRangeValues:
    """All symbol ranges should be 0-based tuples."""

    def test_range_is_four_tuple(self):
        source = "#lang ivy1.7\n\ntype cid\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "cid")
        assert sym is not None
        assert len(sym.range) == 4

    def test_range_values_are_ints(self):
        source = "#lang ivy1.7\n\ntype cid\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "cid")
        assert sym is not None
        for val in sym.range:
            assert isinstance(val, int), f"Range value {val!r} is not int"


# ---------------------------------------------------------------------------
# Test: Module declaration
# ---------------------------------------------------------------------------


class TestModuleDecl:
    """Module declarations produce SymbolKind.Module."""

    def test_module_is_module(self):
        source = """\
#lang ivy1.7

module counter(t) = {
    individual val : t
    action up = {
        val := val + 1;
    }
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "counter")
        assert sym is not None
        assert sym.kind == SymbolKind.Module


# ---------------------------------------------------------------------------
# Test: Definition declaration
# ---------------------------------------------------------------------------


class TestDefinitionDecl:
    """Definition declarations produce SymbolKind.Function."""

    def test_definition_is_function(self):
        source = """\
#lang ivy1.7

type t
individual zero:t
definition zero = 0
"""
        symbols = _parse_and_convert(source)
        # DefinitionDecl defines with auto-generated label like 'def1'
        # Check we have at least a Function-kind symbol from the definition
        func_syms = [s for s in symbols if s.kind == SymbolKind.Function]
        found_def = any("def" in s.name for s in func_syms)
        assert found_def, (
            f"Expected a Function symbol from definition, "
            f"got: {[(s.name, s.kind) for s in symbols]}"
        )


# ---------------------------------------------------------------------------
# Test: Robustness — empty and error cases
# ---------------------------------------------------------------------------


class TestRobustness:
    """Converter handles edge cases gracefully."""

    def test_empty_decls(self):
        source = "#lang ivy1.7\n"
        symbols = _parse_and_convert(source)
        assert isinstance(symbols, list)

    def test_returns_list_of_ivy_symbols(self):
        source = "#lang ivy1.7\n\ntype cid\n"
        symbols = _parse_and_convert(source)
        for sym in symbols:
            assert isinstance(sym, IvySymbol)

    def test_mixin_declarations_produce_method(self):
        source = """\
#lang ivy1.7

type t

object foo = {
    action step(x:t)
}

before foo.step {
    require x ~= x;
}
"""
        symbols = _parse_and_convert(source)
        sym_names = [s.name for s in symbols]
        # Should have 't', 'foo', and the mixin target 'foo.step'
        assert "t" in sym_names
        assert "foo" in sym_names
        # Mixin should produce a Method symbol for the mixee (action monitor)
        mixin_sym = _find_symbol(symbols, "step", SymbolKind.Method)
        assert (
            mixin_sym is not None
        ), f"Expected mixin Method symbol, got: {[(s.name, s.kind) for s in symbols]}"


# ---------------------------------------------------------------------------
# Test: Instance declaration (expanded by parser into ObjectDecl)
# ---------------------------------------------------------------------------


class TestInstanceDecl:
    """Instance declarations are expanded by the parser."""

    def test_instance_produces_module(self):
        source = """\
#lang ivy1.7

type node

module counter(t) = {
    individual val : t
    action up = {
        val := val + 1;
    }
}

instance idx : counter(node)
"""
        symbols = _parse_and_convert(source)
        # Instance 'idx' is expanded to ObjectDecl by the parser
        sym = _find_symbol(symbols, "idx")
        assert sym is not None
        assert sym.kind == SymbolKind.Module


# ---------------------------------------------------------------------------
# Integration test: parse real quic_types.ivy
# ---------------------------------------------------------------------------


class TestQuicTypesIntegration:
    """Parse real quic_types.ivy and verify key symbols exist."""

    def test_find_cid_symbol(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))
        sym = _find_symbol(symbols, "cid")
        assert sym is not None, "Expected 'cid' in quic_types.ivy symbols"
        assert sym.kind == SymbolKind.Class

    def test_find_pkt_num_symbol(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))
        sym = _find_symbol(symbols, "pkt_num")
        assert sym is not None, "Expected 'pkt_num' in quic_types.ivy symbols"
        assert sym.kind == SymbolKind.Class

    def test_find_bit_object(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))
        sym = _find_symbol(symbols, "bit", SymbolKind.Module)
        assert sym is not None, "Expected Module 'bit' in quic_types.ivy symbols"

    def test_find_stream_kind_enum(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))
        sym = _find_symbol(symbols, "stream_kind")
        assert sym is not None, "Expected 'stream_kind' in quic_types.ivy symbols"
        assert sym.kind == SymbolKind.Class
        # Should have detail mentioning enum variants
        assert sym.detail is not None
        assert "unidir" in sym.detail
        assert "bidir" in sym.detail

    def test_multiple_symbols_present(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))
        # quic_types has many types: cid, version, pkt_num, microsecs, etc.
        assert len(symbols) >= 5, f"Expected at least 5 symbols, got {len(symbols)}"

    def test_file_path_set_on_all(self, quic_types_source, quic_types_path):
        symbols = _parse_and_convert(quic_types_source, str(quic_types_path))

        def check_file_path(sym_list, path_str):
            for sym in sym_list:
                assert (
                    sym.file_path == path_str
                ), f"Symbol '{sym.name}' has file_path={sym.file_path!r}"
                check_file_path(sym.children, path_str)

        check_file_path(symbols, str(quic_types_path))


# ---------------------------------------------------------------------------
# Test: Export declaration name without prefix
# ---------------------------------------------------------------------------


class TestExportDecl:
    """Export declarations produce SymbolKind.Event with clean name."""

    def test_export_name_without_prefix(self):
        source = """\
#lang ivy1.7

type t

action send(x:t)

export send
"""
        symbols = _parse_and_convert(source)
        export_syms = [s for s in symbols if s.kind == SymbolKind.Event]
        assert (
            len(export_syms) >= 1
        ), f"Expected at least 1 Event symbol, got: {[(s.name, s.kind) for s in symbols]}"
        # Name should be 'send', not 'export send'
        assert (
            export_syms[0].name == "send"
        ), f"Expected name='send', got name='{export_syms[0].name}'"
        # Detail should carry the export prefix
        assert export_syms[0].detail is not None
        assert "export" in export_syms[0].detail


# ---------------------------------------------------------------------------
# Test: Alias has correct range (not matching comment)
# ---------------------------------------------------------------------------


class TestAliasRange:
    """Alias declarations should point to the actual declaration line."""

    def test_alias_has_correct_range(self):
        source = """\
#lang ivy1.7

type cid
# alias aid mentioned in a comment
alias aid = cid
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "aid")
        assert sym is not None
        # 'alias aid = cid' is on line 5 (1-based), so line_idx=4 (0-based)
        assert (
            sym.range[0] == 4
        ), f"Expected alias on line 4 (0-based), got {sym.range[0]}"


# ---------------------------------------------------------------------------
# Test: Module has non-zero range
# ---------------------------------------------------------------------------


class TestModuleRange:
    """Module declarations should have non-zero ranges."""

    def test_module_has_nonzero_range(self):
        source = """\
#lang ivy1.7

module counter(t) = {
    individual val : t
}
"""
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "counter")
        assert sym is not None
        # Should not be all zeros
        assert sym.range != (
            0,
            0,
            0,
            0,
        ), f"Expected non-zero range for module, got {sym.range}"


# ---------------------------------------------------------------------------
# Test: Attribute declaration
# ---------------------------------------------------------------------------


class TestAttributeDecl:
    """Attribute declarations produce SymbolKind.Constant."""

    def test_attribute_produces_constant_symbol(self):
        source = "#lang ivy1.7\n\nattribute radix = 16\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "radix", SymbolKind.Constant)
        assert (
            sym is not None
        ), f"Expected Constant symbol 'radix', got: {[(s.name, s.kind) for s in symbols]}"
        assert sym.kind == SymbolKind.Constant

    def test_attribute_detail_contains_value(self):
        source = "#lang ivy1.7\n\nattribute radix = 16\n"
        symbols = _parse_and_convert(source)
        sym = _find_symbol(symbols, "radix", SymbolKind.Constant)
        assert sym is not None
        assert sym.detail is not None
        assert "attribute radix = 16" in sym.detail

    def test_attribute_file_path(self):
        source = "#lang ivy1.7\n\nattribute radix = 16\n"
        symbols = _parse_and_convert(source, "my_file.ivy")
        sym = _find_symbol(symbols, "radix", SymbolKind.Constant)
        assert sym is not None
        assert sym.file_path == "my_file.ivy"

    def test_dotted_attribute_stays_at_root(self):
        """Dotted attribute names are callatom references, not nested scopes."""
        source = """\
#lang ivy1.7

type stream_pos
attribute stream_pos.cardinality = 4
"""
        symbols = _parse_and_convert(source)
        # The attribute should be at root level with its full dotted name,
        # NOT nested under the 'stream_pos' type.
        attr = None
        for s in symbols:
            if s.kind == SymbolKind.Constant and "stream_pos" in s.name:
                attr = s
                break
        assert attr is not None, (
            f"Expected Constant attribute with 'stream_pos' in name at root, "
            f"got: {[(s.name, s.kind) for s in symbols]}"
        )
        # Verify it was NOT nested under stream_pos type
        stream_type = _find_symbol(symbols, "stream_pos", SymbolKind.Class)
        if stream_type is not None:
            child_kinds = [c.kind for c in stream_type.children]
            assert (
                SymbolKind.Constant not in child_kinds
            ), "Dotted attribute should NOT be nested under the type"

    def test_deeply_dotted_attribute(self):
        """Deeply dotted attribute names stay at root level."""
        source = """\
#lang ivy1.7

object frame = {
    type this
}
attribute frame.rst_stream.handle.weight = "0.02"
"""
        symbols = _parse_and_convert(source)
        # The attribute should be at root, not nested under frame
        frame_sym = _find_symbol(symbols, "frame", SymbolKind.Module)
        assert frame_sym is not None
        # No Constant children should exist on frame
        const_children = [
            c for c in frame_sym.children if c.kind == SymbolKind.Constant
        ]
        assert len(const_children) == 0, (
            f"Deeply dotted attribute should NOT be nested under 'frame', "
            f"found: {[(c.name, c.kind) for c in const_children]}"
        )
        # Should be findable at root level
        root_constants = [s for s in symbols if s.kind == SymbolKind.Constant]
        assert len(root_constants) >= 1, "Expected at least one root-level Constant"
