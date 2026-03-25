"""Full-mode requirement extraction from Ivy AST bodies.

Walks ``ActionDecl`` bodies in the parsed AST to find ``RequiresAction``,
``EnsuresAction``, ``AssumeAction``, ``AssertAction`` and ``AssignAction``
nodes.  Cross-references ``MixinDecl`` entries to resolve which action
each monitor is attached to.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ivy_lsp.core.analysis.requirement_graph import RequirementNode
from ivy_lsp.core.parsing.ast_to_symbols import is_from_included_file

logger = logging.getLogger(__name__)

# Pattern to detect mangled mixin action names: "foo[before1]", "bar[after2]",
# "baz[around1]", "qux[implement0]"
_MIXIN_NAME_RE = re.compile(r"^([^[]+)\[(before|after|around|implement)(\d+)\]$")


def extract_requirements_full(
    ast_obj: Any,
    filepath: str,
    source: str,
) -> Tuple[List[RequirementNode], List[Tuple[str, str, int]]]:
    """Extract all requirements from AST using full parser.

    Returns:
        A tuple of (requirements, writes) where writes are
        ``(var_name, filepath, line)`` triples for assignments.
    """
    if ast_obj is None or not hasattr(ast_obj, "decls"):
        return [], []

    abs_filepath = os.path.abspath(filepath) if filepath else None
    mixin_map = _build_mixin_map(ast_obj, filepath)
    source_lines = source.split("\n")
    requirements: List[RequirementNode] = []
    writes: List[Tuple[str, str, int]] = []

    for decl in ast_obj.decls:
        if is_from_included_file(decl, abs_filepath):
            continue
        try:
            _process_decl(decl, filepath, source_lines, mixin_map, requirements, writes)
        except Exception:
            logger.warning(
                "Failed to extract requirements from %s in %s",
                type(decl).__name__,
                filepath,
                exc_info=True,
            )

    return requirements, writes


def extract_exports_imports_full(
    ast_obj: Any,
    filepath: str,
    source: str,
) -> "ExportImportInfo":
    """Extract export/import declarations from full AST.

    Walks ``ast_obj.decls`` looking for ``ExportDecl`` and ``ImportDecl``
    nodes.  Falls back to light-mode regex extraction if the ``ivy``
    package is not importable.

    Returns:
        An ``ExportImportInfo`` with names and 0-based line numbers.
    """
    from ivy_lsp.core.analysis.test_scope import ExportImportInfo

    if ast_obj is None or not hasattr(ast_obj, "decls"):
        return ExportImportInfo(file=filepath)

    try:
        import ivy.ivy_ast as ia
    except ImportError:
        from ivy_lsp.core.analysis.light_mode_extractor import (
            extract_exports_imports_light,
        )

        return extract_exports_imports_light(source, filepath)

    exports: List[str] = []
    imports: List[str] = []
    export_lines: Dict[str, int] = {}
    import_lines: Dict[str, int] = {}

    for decl in ast_obj.decls:
        try:
            if isinstance(decl, ia.ExportDecl):
                line = _get_line(decl)
                for defn in decl.args:
                    atom = defn.args[0] if defn.args else None
                    name = _atom_to_name(atom) if atom else None
                    if name:
                        exports.append(name)
                        export_lines[name] = line

            elif isinstance(decl, ia.ImportDecl):
                line = _get_line(decl)
                for defn in decl.args:
                    atom = defn.args[0] if defn.args else None
                    name = _atom_to_name(atom) if atom else None
                    if name:
                        imports.append(name)
                        import_lines[name] = line
        except Exception:
            logger.warning(
                "Failed to extract export/import from %s in %s",
                type(decl).__name__,
                filepath,
                exc_info=True,
            )

    return ExportImportInfo(
        file=filepath,
        exports=exports,
        imports=imports,
        export_lines=export_lines,
        import_lines=import_lines,
    )


def _process_decl(
    decl: Any,
    filepath: str,
    source_lines: List[str],
    mixin_map: Dict[str, str],
    requirements: List[RequirementNode],
    writes: List[Tuple[str, str, int]],
) -> None:
    """Process a single declaration, extracting requirements if applicable."""
    import ivy.ivy_ast as ia

    if not isinstance(decl, ia.ActionDecl):
        return

    defs = decl.defines()
    if not defs:
        return

    action_name = defs[0][0]

    # Determine if this is a mixin action
    m = _MIXIN_NAME_RE.match(action_name)
    if m:
        base_name = m.group(1)
        mixin_kind = m.group(2)  # "before", "after", "around", or "implement"
        monitor_action = mixin_map.get(action_name, base_name)
    else:
        mixin_kind = "direct"
        monitor_action = action_name

    # Get the action body
    body = _get_action_body(decl)
    if body is None:
        return

    # Walk the body
    _walk_body(
        body,
        filepath,
        source_lines,
        monitor_action,
        mixin_kind,
        requirements,
        writes,
    )


def _get_action_body(decl: Any) -> Any:
    """Extract the body from an ActionDecl."""
    try:
        action_def = decl.args[0]  # ActionDef
        body = getattr(action_def, "body", None)
        if body is not None:
            return body
        # Some action defs store body in args
        if hasattr(action_def, "args") and len(action_def.args) > 0:
            return action_def.args[-1]
    except (IndexError, AttributeError):
        pass
    return None


def _walk_body(
    node: Any,
    filepath: str,
    source_lines: List[str],
    monitor_action: str,
    mixin_kind: str,
    requirements: List[RequirementNode],
    writes: List[Tuple[str, str, int]],
) -> None:
    """Recursively walk an action body collecting requirements and writes."""
    if node is None:
        return

    try:
        import ivy.ivy_actions as iact
    except ImportError:
        logger.debug("ivy.ivy_actions not available for body walking")
        return

    # Requirement statements
    if isinstance(node, iact.RequiresAction):
        _add_requirement(
            node,
            "require",
            filepath,
            source_lines,
            monitor_action,
            mixin_kind,
            requirements,
        )
        return

    if isinstance(node, iact.EnsuresAction):
        _add_requirement(
            node,
            "ensure",
            filepath,
            source_lines,
            monitor_action,
            mixin_kind,
            requirements,
        )
        return

    if isinstance(node, iact.AssumeAction):
        _add_requirement(
            node,
            "assume",
            filepath,
            source_lines,
            monitor_action,
            mixin_kind,
            requirements,
        )
        return

    if isinstance(node, iact.AssertAction):
        _add_requirement(
            node,
            "assert",
            filepath,
            source_lines,
            monitor_action,
            mixin_kind,
            requirements,
        )
        return

    # Assignment: track state variable writes
    if isinstance(node, iact.AssignAction):
        _add_write(node, filepath, writes)
        # Don't return — assignment may not have sub-actions but be safe
        return

    # Compound: Sequence, IfAction, WhileAction, LocalAction
    if isinstance(node, iact.Sequence):
        for arg in node.args:
            _walk_body(
                arg,
                filepath,
                source_lines,
                monitor_action,
                mixin_kind,
                requirements,
                writes,
            )
        return

    if isinstance(node, iact.IfAction):
        # args[0] = condition, args[1] = then branch, args[2]? = else
        for arg in node.args[1:]:
            _walk_body(
                arg,
                filepath,
                source_lines,
                monitor_action,
                mixin_kind,
                requirements,
                writes,
            )
        return

    if isinstance(node, iact.LocalAction):
        # args[:-1] = local vars, args[-1] = body
        if node.args:
            _walk_body(
                node.args[-1],
                filepath,
                source_lines,
                monitor_action,
                mixin_kind,
                requirements,
                writes,
            )
        return

    # CallAction: walk into callee's body if inline
    if isinstance(node, iact.CallAction):
        return

    # Generic: try to walk args for any other compound type
    for arg in getattr(node, "args", ()):
        if hasattr(arg, "args"):
            _walk_body(
                arg,
                filepath,
                source_lines,
                monitor_action,
                mixin_kind,
                requirements,
                writes,
            )


def _add_requirement(
    node: Any,
    kind: str,
    filepath: str,
    source_lines: List[str],
    monitor_action: str,
    mixin_kind: str,
    requirements: List[RequirementNode],
) -> None:
    """Create a RequirementNode from an AST node and add to the list."""
    line = _get_line(node)
    col = 0
    formula_text = _formula_to_text(node)
    bracket_tags = _extract_bracket_tags(source_lines, line)

    req_id = f"{filepath}:{line}"
    requirements.append(
        RequirementNode(
            id=req_id,
            kind=kind,
            formula_text=formula_text,
            line=line,
            col=col,
            file=filepath,
            monitor_action=monitor_action,
            mixin_kind=mixin_kind,
            bracket_tags=bracket_tags,
            ast_node=node.args[0] if node.args else None,
        )
    )


def _add_write(
    node: Any,
    filepath: str,
    writes: List[Tuple[str, str, int]],
) -> None:
    """Record an assignment target as a state variable write."""
    if not node.args:
        return
    lhs = node.args[0]
    var_name = getattr(lhs, "relname", None) or getattr(lhs, "rep", None)
    if var_name:
        line = _get_line(node)
        writes.append((var_name, filepath, line))


def _get_line(node: Any) -> int:
    """Get 0-based line number from an AST node."""
    lineno = getattr(node, "lineno", None)
    if lineno is not None:
        line = getattr(lineno, "line", None)
        if isinstance(line, int) and line > 0:
            return line - 1
    return 0


def _formula_to_text(node: Any) -> str:
    """Convert a requirement node's formula to text."""
    if not node.args:
        return str(node)
    formula = node.args[0]
    try:
        return str(formula)
    except Exception:
        return repr(formula)


def _extract_bracket_tags(source_lines: List[str], line: int) -> List[str]:
    """Parse bracket annotations from comment suffix.

    Delegates to rfc_annotations.parse_rfc_tags for the actual parsing.
    """
    if line < 0 or line >= len(source_lines):
        return []
    from ivy_lsp.core.semantic.rfc_annotations import parse_rfc_tags

    return parse_rfc_tags(source_lines[line])


def _build_mixin_map(ast_obj: Any, filepath: Optional[str] = None) -> Dict[str, str]:
    """Map mangled mixer names to mixee (monitored action) names.

    Scans ``MixinDecl`` entries in ``ast.decls``.  Each ``MixinDecl``
    contains a ``MixinBeforeDef`` or ``MixinAfterDef`` linking the
    mixer action to the mixee action.

    Declarations originating from included files are skipped so that
    mixin mappings from library files do not leak into the test file's
    requirement graph.
    """
    import ivy.ivy_ast as ia

    abs_filepath = os.path.abspath(filepath) if filepath else None
    mixin_map: Dict[str, str] = {}

    for decl in ast_obj.decls:
        if not isinstance(decl, ia.MixinDecl):
            continue
        if is_from_included_file(decl, abs_filepath):
            continue

        try:
            mixin_def = decl.args[0]  # MixinBeforeDef or MixinAfterDef
            mixer = getattr(mixin_def, "mixer", None)
            mixee = getattr(mixin_def, "mixee", None)

            if mixer is None or mixee is None:
                # Try args: args[0] = mixer, args[1] = mixee
                if hasattr(mixin_def, "args") and len(mixin_def.args) >= 2:
                    mixer = mixin_def.args[0]
                    mixee = mixin_def.args[1]

            mixer_name = _atom_to_name(mixer)
            mixee_name = _atom_to_name(mixee)

            if mixer_name and mixee_name:
                mixin_map[mixer_name] = mixee_name
        except Exception:
            logger.warning(
                "Failed to extract mixin mapping from %s",
                type(decl).__name__,
                exc_info=True,
            )

    return mixin_map


def _atom_to_name(atom: Any) -> Optional[str]:
    """Extract a name string from an Atom-like AST node."""
    if atom is None:
        return None
    relname = getattr(atom, "relname", None)
    if relname:
        return relname
    rep = getattr(atom, "rep", None)
    if rep:
        return rep
    return str(atom) if atom else None
