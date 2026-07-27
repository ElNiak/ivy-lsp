"""Requirement extraction for light mode (no Z3/full parser).

Dispatches to the lexer-based extractor when the PLY lexer is available,
falling back to regex-based extraction otherwise.

Regex patterns here intentionally stay as regex — they already implement a
lexer-to-regex cascade for requirement extraction, which is separate from the
symbol extraction cascade in ivy_lsp.core.parsing.tiered_extractor.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from ivy_lsp.core.analysis.requirement_graph import RequirementNode

if TYPE_CHECKING:
    from ivy_lsp.core.analysis.test_scope import ExportImportInfo
from ivy_lsp.core.semantic.rfc_annotations import parse_rfc_tags

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lexer availability check
# ---------------------------------------------------------------------------

_LEXER_AVAILABLE: bool = False


def _check_lexer() -> bool:
    """Check (and cache) whether the PLY lexer is importable."""
    global _LEXER_AVAILABLE
    if _LEXER_AVAILABLE:
        return True
    try:
        from ivy_lsp.core.parsing.token_stream import tokenize_ivy  # noqa: F401

        _LEXER_AVAILABLE = True
    except ImportError:
        _LEXER_AVAILABLE = False
    return _LEXER_AVAILABLE


# Attempt import at module load time so the flag is set early.
_check_lexer()

# ---------------------------------------------------------------------------
# Regex patterns (used by fallback path)
# ---------------------------------------------------------------------------

MONITOR_RE = re.compile(
    r"\b(before|after|around|implement)\s+([\w.]+)\s*(?:\([^)]*\))?\s*\{",
    re.MULTILINE,
)
REQUIRE_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+(.+?);\s*(?:#\s*\[([\w:.,\s]+)\])?\s*$",
    re.MULTILINE,
)
ASSIGN_RE = re.compile(r"^\s*([\w.]+(?:\([^)]*\))?)\s*:=", re.MULTILINE)

# Direct action body pattern: "action name(...) = { ... }"
ACTION_RE = re.compile(
    r"\baction\s+([\w.]+)\s*(?:\([^)]*\))?\s*(?:returns\s*\([^)]*\))?\s*=\s*\{",
    re.MULTILINE,
)

# Export/import declaration patterns (bare and action forms)
EXPORT_RE = re.compile(
    r"^\s*(?:action\s+)?export\s+([\w.]+)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(
    r"^\s*(?:action\s+)?import\s+([\w.]+)",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Public API (dispatch)
# ---------------------------------------------------------------------------


def extract_requirements_light(
    source: str,
    filepath: str,
    known_state_vars: Optional[Set[str]] = None,
    token_stream: Optional[Any] = None,
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Dispatch to lexer or regex extraction.

    When the PLY lexer is available, delegates to
    ``lexer_requirement_extractor.extract_requirements_lexer`` for
    higher fidelity.  Falls back to regex-based extraction if the
    lexer is unavailable or encounters errors (e.g. native blocks
    with C++ string literals containing ``>>>``).
    """
    if _LEXER_AVAILABLE:
        from ivy_lsp.core.analysis.lexer_requirement_extractor import (
            extract_requirements_lexer,
        )

        # If caller provided a token_stream with errors, skip lexer path.
        if token_stream is not None and token_stream.error_info is not None:
            return _extract_requirements_regex(source, filepath, known_state_vars)

        # Try lexer; on tokenization error, fall back to regex.
        from ivy_lsp.core.parsing.token_stream import tokenize_ivy

        if token_stream is None:
            token_stream = tokenize_ivy(source, filepath)

        if token_stream.error_info is not None:
            logger.debug(
                "Lexer had errors for %s; falling back to regex extraction",
                filepath,
            )
            return _extract_requirements_regex(source, filepath, known_state_vars)

        return extract_requirements_lexer(
            source, filepath, known_state_vars, token_stream=token_stream
        )
    return _extract_requirements_regex(source, filepath, known_state_vars)


def extract_exports_imports_light(
    source: str,
    filepath: str,
    token_stream: Optional[Any] = None,
) -> "ExportImportInfo":
    """Dispatch to lexer or regex export/import extraction."""
    if _LEXER_AVAILABLE:
        # Fall back to regex if token stream has errors.
        if token_stream is not None and token_stream.error_info is not None:
            return _extract_exports_imports_regex(source, filepath)

        from ivy_lsp.core.analysis.lexer_requirement_extractor import (
            extract_exports_imports_lexer,
        )

        return extract_exports_imports_lexer(
            source, filepath, token_stream=token_stream
        )
    return _extract_exports_imports_regex(source, filepath)


# ---------------------------------------------------------------------------
# Regex fallback: requirements
# ---------------------------------------------------------------------------


def _extract_requirements_regex(
    source: str,
    filepath: str,
    known_state_vars: Optional[Set[str]] = None,
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Regex-based requirement extraction (fallback path)."""
    if not source:
        return [], []

    source_lines = source.split("\n")
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    # Phase 1: Find monitor blocks (before/after/around)
    monitor_blocks = _find_monitor_blocks(source)

    # Phase 2: Find direct action bodies
    action_blocks = _find_action_blocks(source)

    # Phase 3: Extract requirements from all blocks
    all_blocks = monitor_blocks + action_blocks
    for block in all_blocks:
        block_reqs, block_writes = _extract_from_block(block, filepath, source_lines)
        requirements.extend(block_reqs)
        writes.extend(block_writes)

    return requirements, writes


def _find_monitor_blocks(
    source: str,
) -> List[Dict[str, Any]]:
    """Find before/after/around monitor blocks using regex + brace depth."""
    blocks = []

    for m in MONITOR_RE.finditer(source):
        mixin_kind_raw = m.group(1)  # "before", "after", "around"
        monitor_action = m.group(2)  # action name
        open_brace_offset = m.end() - 1  # position of the opening brace

        mixin_kind = mixin_kind_raw  # preserve as-is

        # Find the matching closing brace using depth tracking
        start_line = source[: m.start()].count("\n")
        body_start = m.end()
        body_end = _find_matching_brace(source, open_brace_offset)

        if body_end is None:
            logger.debug(
                "Unterminated brace in monitor block for %s at line %d",
                monitor_action,
                start_line,
            )
            continue

        blocks.append(
            {
                "monitor_action": monitor_action,
                "mixin_kind": mixin_kind,
                "body_text": source[body_start:body_end],
                "body_start_offset": body_start,
                "start_line": start_line,
            }
        )

    return blocks


def _find_action_blocks(
    source: str,
) -> List[Dict[str, Any]]:
    """Find direct action body blocks: 'action name(...) = { ... }'."""
    blocks = []

    for m in ACTION_RE.finditer(source):
        action_name = m.group(1)
        open_brace_offset = m.end() - 1

        start_line = source[: m.start()].count("\n")
        body_start = m.end()
        body_end = _find_matching_brace(source, open_brace_offset)

        if body_end is None:
            logger.debug(
                "Unterminated brace in action block for %s at line %d",
                action_name,
                start_line,
            )
            continue

        blocks.append(
            {
                "monitor_action": action_name,
                "mixin_kind": "direct",
                "body_text": source[body_start:body_end],
                "body_start_offset": body_start,
                "start_line": start_line,
            }
        )

    return blocks


def _find_matching_brace(source: str, open_pos: int) -> Optional[int]:
    """Find the position of the matching closing brace.

    *open_pos* points to the opening ``{``.  Returns the position
    just before the closing ``}``, or ``None`` if unmatched.

    Handles Ivy line comments (``#``) and native code blocks
    (``<<<`` ... ``>>>``) which may contain C++ braces that must
    not affect the depth counter.
    """
    depth = 1
    i = open_pos + 1

    while i < len(source) and depth > 0:
        ch = source[i]

        # Skip Ivy line comments
        if ch == "#":
            nl = source.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue

        # Skip native code blocks: <<< ... >>>
        # Must skip C++ string literals that might contain >>>
        if ch == "<" and source[i : i + 3] == "<<<":
            j = i + 3
            while j < len(source):
                if source[j] == '"':
                    # Skip C++ string literal
                    j += 1
                    while j < len(source) and source[j] != '"':
                        if source[j] == "\\":
                            j += 1  # skip escaped char
                        j += 1
                    j += 1  # skip closing quote
                elif source[j : j + 3] == ">>>":
                    j += 3
                    break
                else:
                    j += 1
            else:
                break  # unterminated native block
            i = j
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i

        i += 1

    return None


def _extract_from_block(
    block: Dict[str, Any],
    filepath: str,
    source_lines: List[str],
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Extract requirements and writes from a single block."""
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    body_text = block["body_text"]
    body_start_offset = block["body_start_offset"]
    monitor_action = block["monitor_action"]
    mixin_kind = block["mixin_kind"]

    # Find require/ensure/assume/assert in the block
    for m in REQUIRE_RE.finditer(body_text):
        kind = m.group(1)
        formula_text = m.group(2).strip()
        raw_tags = m.group(3)  # may be None

        bracket_tags: List[str] = []
        if raw_tags:
            bracket_tags = parse_rfc_tags(f"# [{raw_tags}]")

        # Calculate absolute line number
        rel_offset = m.start()
        abs_offset = body_start_offset + rel_offset
        line = _offset_to_line(abs_offset, source_lines)

        req_id = f"{filepath}:{line}"
        requirements.append(
            RequirementNode(
                id=req_id,
                kind=kind,
                formula_text=formula_text,
                line=line,
                col=0,
                file=filepath,
                monitor_action=monitor_action,
                mixin_kind=mixin_kind,
                bracket_tags=bracket_tags,
            )
        )

    # Find assignments
    for m in ASSIGN_RE.finditer(body_text):
        var_name = m.group(1)
        # Strip function call syntax from LHS: "foo(X)" -> "foo"
        paren_idx = var_name.find("(")
        if paren_idx >= 0:
            var_name = var_name[:paren_idx]

        rel_offset = m.start()
        abs_offset = body_start_offset + rel_offset
        line = _offset_to_line(abs_offset, source_lines)
        writes.append((var_name, filepath, line))

    return requirements, writes


def _offset_to_line(offset: int, source_lines: List[str]) -> int:
    """Convert a character offset to a 0-based line number."""
    source = "\n".join(source_lines)
    return min(source[:offset].count("\n"), max(0, len(source_lines) - 1))


# ---------------------------------------------------------------------------
# Regex fallback: exports/imports
# ---------------------------------------------------------------------------


def _extract_exports_imports_regex(
    source: str,
    filepath: str,
) -> "ExportImportInfo":
    """Regex-based export/import extraction (fallback path)."""
    from ivy_lsp.core.analysis.test_scope import ExportImportInfo

    exports: List[str] = []
    imports: List[str] = []
    export_lines: Dict[str, int] = {}
    import_lines: Dict[str, int] = {}

    for m in EXPORT_RE.finditer(source):
        # Skip matches inside Ivy comments (# is the comment character).
        line_start = source.rfind("\n", 0, m.start()) + 1
        line_text = source[line_start : m.start()]
        if "#" in line_text:
            continue
        name = m.group(1)
        line = source[: m.start()].count("\n")
        exports.append(name)
        export_lines[name] = line

    for m in IMPORT_RE.finditer(source):
        line_start = source.rfind("\n", 0, m.start()) + 1
        line_text = source[line_start : m.start()]
        if "#" in line_text:
            continue
        name = m.group(1)
        line = source[: m.start()].count("\n")
        imports.append(name)
        import_lines[name] = line

    return ExportImportInfo(
        file=filepath,
        exports=exports,
        imports=imports,
        export_lines=export_lines,
        import_lines=import_lines,
    )
