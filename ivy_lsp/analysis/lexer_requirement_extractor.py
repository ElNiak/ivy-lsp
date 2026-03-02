"""Lexer-based requirement extraction (medium fidelity).

Walks PLY lexer tokens to extract requirements, assignments, and
export/import declarations.  Replaces regex-based extraction when
the PLY lexer is available (requires only PLY, not Z3).

Bracket tags remain regex-based since comments are stripped by the lexer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from ivy_lsp.analysis.requirement_graph import RequirementNode
from ivy_lsp.parsing.token_stream import TokenStream, tokenize_ivy
from ivy_lsp.semantic.rfc_annotations import parse_rfc_tags

if TYPE_CHECKING:
    from ivy_lsp.analysis.test_scope import ExportImportInfo

logger = logging.getLogger(__name__)

_MONITOR_TOKENS = frozenset({"BEFORE", "AFTER", "AROUND", "IMPLEMENT"})
_REQUIREMENT_TOKENS = frozenset({"REQUIRE", "ENSURE", "ASSUME", "ASSERT"})
_REQ_KIND_MAP = {
    "REQUIRE": "require",
    "ENSURE": "ensure",
    "ASSUME": "assume",
    "ASSERT": "assert",
}


def extract_requirements_lexer(
    source: str,
    filepath: str,
    known_state_vars: Optional[Set[str]] = None,
    token_stream: Optional[TokenStream] = None,
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Token-based requirement extraction.

    Same return signature as extract_requirements_light:
    (requirements, writes) where writes are (var_name, filepath, line).
    """
    if not source or not source.strip():
        return [], []

    if token_stream is None:
        token_stream = tokenize_ivy(source, filepath)

    tokens = token_stream.tokens
    lines = token_stream.lines
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    blocks = _find_blocks(tokens, source)
    for block in blocks:
        _extract_from_block(
            block, tokens, source, lines, filepath, requirements, writes
        )

    return requirements, writes


def _read_dotted_name(tokens: list, start: int) -> Tuple[Optional[str], int]:
    """Read a potentially dot-separated name starting at *start*.

    Returns (name, next_index) or (None, start).
    Accepts PRESYMBOL and reserved-word tokens that are valid identifiers
    (same logic as fallback_scanner._is_name_token).
    """
    if start >= len(tokens):
        return None, start

    tok = tokens[start]
    if tok.type != "PRESYMBOL" and not (tok.value and tok.value.isidentifier()):
        return None, start

    parts = [tok.value]
    i = start + 1
    while i + 1 < len(tokens) and tokens[i].type == "DOT":
        nxt = tokens[i + 1]
        if nxt.type == "PRESYMBOL" or (nxt.value and nxt.value.isidentifier()):
            parts.append(nxt.value)
            i += 2
        else:
            break

    return ".".join(parts), i


def _find_blocks(tokens: list, source: str) -> List[Dict[str, Any]]:
    """Find monitor blocks and action body blocks from the token stream."""
    blocks: List[Dict[str, Any]] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]

        # Monitor: BEFORE/AFTER/AROUND/IMPLEMENT name ... {
        if tok.type in _MONITOR_TOKENS:
            mixin_kind = tok.type.lower()
            name, next_i = _read_dotted_name(tokens, i + 1)
            if name is None:
                i += 1
                continue
            # Skip past params/etc until opening brace
            j = next_i
            while j < len(tokens) and tokens[j].type != "LCB":
                j += 1
            if j >= len(tokens):
                i += 1
                continue
            body_start = j + 1
            body_end = _find_matching_rcb(tokens, j)
            if body_end is None:
                i += 1
                continue
            blocks.append({
                "monitor_action": name,
                "mixin_kind": mixin_kind,
                "body_start_idx": body_start,
                "body_end_idx": body_end,
            })
            i = body_end + 1
            continue

        # Action: ACTION name ... = {
        if tok.type == "ACTION":
            name, next_i = _read_dotted_name(tokens, i + 1)
            if name is None:
                i += 1
                continue
            j = next_i
            found_eq = False
            while j < len(tokens):
                if tokens[j].type == "EQ":
                    found_eq = True
                    j += 1
                    # Skip optional whitespace/tokens between = and {
                    while j < len(tokens) and tokens[j].type != "LCB":
                        j += 1
                    break
                if tokens[j].type == "LCB" and not found_eq:
                    # Brace without = means this is not an action body
                    break
                j += 1
            if not found_eq or j >= len(tokens) or tokens[j].type != "LCB":
                i += 1
                continue
            body_start = j + 1
            body_end = _find_matching_rcb(tokens, j)
            if body_end is None:
                i += 1
                continue
            blocks.append({
                "monitor_action": name,
                "mixin_kind": "direct",
                "body_start_idx": body_start,
                "body_end_idx": body_end,
            })
            i = body_end + 1
            continue

        i += 1

    return blocks


def _find_matching_rcb(tokens: list, open_idx: int) -> Optional[int]:
    """Find the index of the matching RCB for the LCB at open_idx.

    Because the lexer already handles comments and native blocks,
    we simply count LCB/RCB tokens.
    """
    depth = 1
    i = open_idx + 1
    while i < len(tokens) and depth > 0:
        if tokens[i].type == "LCB":
            depth += 1
        elif tokens[i].type == "RCB":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _extract_from_block(
    block: Dict[str, Any],
    tokens: list,
    source: str,
    lines: List[str],
    filepath: str,
    requirements: List[RequirementNode],
    writes: List[Tuple[str, str, int]],
) -> None:
    """Extract requirements and writes from a single block's token range."""
    monitor_action = block["monitor_action"]
    mixin_kind = block["mixin_kind"]
    start = block["body_start_idx"]
    end = block["body_end_idx"]

    i = start
    while i < end:
        tok = tokens[i]

        # Requirement: REQUIRE/ENSURE/ASSUME/ASSERT ... SEMI
        if tok.type in _REQUIREMENT_TOKENS:
            kind = _REQ_KIND_MAP[tok.type]
            line_0based = tok.lineno - 1

            # Collect formula: tokens from i+1 until SEMI
            formula_start = i + 1
            j = formula_start
            while j < end and tokens[j].type != "SEMI":
                j += 1

            # Extract formula text from source using token lexpos
            if formula_start < j:
                first_tok = tokens[formula_start]
                last_tok = tokens[j - 1]
                f_start = first_tok.lexpos
                f_end = last_tok.lexpos + len(str(last_tok.value))
                formula_text = source[f_start:f_end].strip()
            else:
                formula_text = ""

            # Bracket tags from source line (comments invisible to lexer)
            bracket_tags: List[str] = []
            if 0 <= line_0based < len(lines):
                bracket_tags = parse_rfc_tags(lines[line_0based])

            req_id = f"{filepath}:{line_0based}"
            requirements.append(
                RequirementNode(
                    id=req_id,
                    kind=kind,
                    formula_text=formula_text,
                    line=line_0based,
                    col=0,
                    file=filepath,
                    monitor_action=monitor_action,
                    mixin_kind=mixin_kind,
                    bracket_tags=bracket_tags,
                )
            )
            i = j + 1
            continue

        # Assignment: ... ASSIGN (:=)
        if tok.type == "ASSIGN" and i > start:
            lhs_tok = tokens[i - 1]
            var_name = str(lhs_tok.value)
            # Walk backward to collect dotted prefix
            k = i - 1
            parts = [var_name]
            while k >= start + 2 and tokens[k - 1].type == "DOT":
                parts.insert(0, str(tokens[k - 2].value))
                k -= 2
            if len(parts) > 1:
                var_name = ".".join(parts)
            # Strip function call syntax from LHS: "foo(X)" -> "foo"
            paren = var_name.find("(")
            if paren >= 0:
                var_name = var_name[:paren]
            line_0based = tok.lineno - 1
            writes.append((var_name, filepath, line_0based))

        i += 1


def extract_exports_imports_lexer(
    source: str,
    filepath: str,
    token_stream: Optional[TokenStream] = None,
) -> "ExportImportInfo":
    """Token-based export/import extraction.

    Same return type as extract_exports_imports_light.
    """
    from ivy_lsp.analysis.test_scope import ExportImportInfo

    if not source or not source.strip():
        return ExportImportInfo(file=filepath)

    if token_stream is None:
        token_stream = tokenize_ivy(source, filepath)

    tokens = token_stream.tokens
    exports: List[str] = []
    imports: List[str] = []
    export_lines: Dict[str, int] = {}
    import_lines: Dict[str, int] = {}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "EXPORT":
            name, next_i = _read_dotted_name(tokens, i + 1)
            if name:
                exports.append(name)
                export_lines[name] = tok.lineno - 1
                i = next_i
                continue
        if tok.type == "IMPORT":
            name, next_i = _read_dotted_name(tokens, i + 1)
            if name:
                imports.append(name)
                import_lines[name] = tok.lineno - 1
                i = next_i
                continue
        i += 1

    return ExportImportInfo(
        file=filepath,
        exports=exports,
        imports=imports,
        export_lines=export_lines,
        import_lines=import_lines,
    )
