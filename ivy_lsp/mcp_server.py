"""MCP server mode for ivy-lsp.

Exposes Ivy verification tools, structured diagnostics, include graph,
and fast lint as MCP tools via the Model Context Protocol. Shares the
same parsing and indexing code as the LSP server.

Usage:
    python -m ivy_lsp --mcp
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import time
from typing import Any

from ivy_lsp.utils.validation import validate_ivy_param as _validate_ivy_param

logger = logging.getLogger(__name__)


def _validate_path(root: str, relative_path: str) -> str:
    """Resolve *relative_path* under *root*, rejecting traversal escapes."""
    abs_path = os.path.realpath(os.path.join(root, relative_path))
    real_root = os.path.realpath(root)
    if not abs_path.startswith(real_root + os.sep) and abs_path != real_root:
        raise ValueError(f"Path escapes workspace root: {relative_path}")
    return abs_path


def _find_ivy_files(root: str, exclude_dirs: set[str] | None = None) -> list[str]:
    """Walk the project and return relative paths to all .ivy files.

    NOTE: Default exclude_dirs overlaps with _EXCLUDED_DIR_BASENAMES in
    indexer/include_resolver.py — consider aligning when adding entries.
    """
    if exclude_dirs is None:
        exclude_dirs = {
            ".git", ".venv", "venv", "node_modules", "__pycache__",
            "build", "dist", "submodules", "test",
        }
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fname in filenames:
            if fname.endswith(".ivy"):
                results.append(os.path.relpath(
                    os.path.join(dirpath, fname), root
                ))
    return results


def _parse_ivy_check_output(output: str) -> list[dict[str, Any]]:
    """Parse ivy_check output into structured diagnostics."""
    diagnostics: list[dict[str, Any]] = []
    for line in output.splitlines():
        m = re.match(r"(.*?):(\d+):\s*(error|warning):\s*(.*)", line)
        if m:
            diagnostics.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "severity": m.group(3),
                "message": m.group(4),
            })
    return diagnostics


def _check_structural_issues(source: str, filepath: str) -> list[dict[str, Any]]:
    """Fast structural checks without full parsing."""
    diags: list[dict[str, Any]] = []
    lines = source.split("\n")

    # Missing #lang header
    stripped = source.lstrip()
    if not stripped.startswith("#lang"):
        diags.append({
            "line": 1, "severity": "warning",
            "message": "Missing '#lang ivy1.7' header", "source": "ivy-lint",
        })

    # Unmatched braces
    depth = 0
    for i, line_text in enumerate(lines):
        code = line_text if line_text.strip().startswith("#lang") else line_text.split("#")[0]
        for ch in code:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if depth < 0:
                diags.append({
                    "line": i + 1, "severity": "error",
                    "message": "Unmatched closing brace", "source": "ivy-lint",
                })
                depth = 0
    if depth > 0:
        diags.append({
            "line": len(lines), "severity": "error",
            "message": f"Unmatched opening brace ({depth} unclosed)",
            "source": "ivy-lint",
        })

    # Unresolved includes
    parent_dir = os.path.dirname(filepath)
    for match in re.finditer(r"^include\s+(\w+)", source, re.MULTILINE):
        inc_name = match.group(1)
        candidate = os.path.join(parent_dir, inc_name + ".ivy")
        if not os.path.isfile(candidate):
            line_no = source[: match.start()].count("\n") + 1
            diags.append({
                "line": line_no, "severity": "warning",
                "message": f"Unresolved include: {inc_name}",
                "source": "ivy-lint",
            })

    return diags


def start_mcp(
    workspace_root: str | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
    docker_image: str | None = None,
    base_path: str | None = None,
    _return_app: bool = False,
) -> Any:
    """Start the MCP server exposing Ivy tools.

    Args:
        workspace_root: Root directory for the workspace.
        semantic_model: Optional SemanticModel for shared-process mode.
        requirement_graph: Optional RequirementGraph (or ScopedRequirementModel)
            for visualization tools. When provided, enables ivy_action_requirements,
            ivy_model_summary, and ivy_coverage_gaps MCP tools.
        docker_image: Docker image for Ivy compilation (e.g. "panther_ivy:latest").
            When set, compilation tools use Docker instead of native subprocess.
        base_path: Base protocol-testing path for compile command generation.
        _return_app: Internal flag for testing. When True, returns the FastMCP
            instance without starting the server.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        logger.critical(
            "MCP mode requires the 'mcp' package. "
            "Install with: pip install ivy-lsp[mcp]"
        )
        sys.exit(1)

    root = workspace_root or os.getcwd()

    # Create executor for Docker-aware compilation
    executor = None
    if docker_image:
        try:
            from panther_ivy.api.executor import IvyExecutor

            executor = IvyExecutor(docker_image=docker_image)
            logger.info(
                "Docker executor configured with image: %s", docker_image
            )
        except ImportError:
            logger.warning(
                "panther_ivy.api.executor not available; "
                "falling back to native subprocess"
            )
    mcp = FastMCP(
        "ivy-lsp",
        instructions=(
            "Ivy Language Server MCP tools for formal verification. "
            "Provides verification (ivy_check), compilation (ivyc), "
            "model inspection (ivy_show), fast linting, include graph analysis, "
            "semantic traceability (RFC coverage, impact analysis, cross-references), "
            "and model visualization (action requirements, summary table, coverage gaps, "
            "action dependency graph, state machine view)."
        ),
    )

    import threading
    _model_lock = threading.Lock()

    @mcp.tool()
    def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Run ivy_check on an Ivy file to verify formal properties.

        Returns structured diagnostics with file, line, severity, and message.

        Args:
            relative_path: Relative path to the .ivy file to check.
            isolate: Optional isolate name to check in isolation.
        """
        import subprocess

        try:
            abs_path = _validate_path(root, relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        cmd = ["ivy_check"]
        if isolate:
            try:
                _validate_ivy_param(isolate)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivy_check not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 120s"})

        duration = time.monotonic() - start
        raw_output = result.stderr + "\n" + result.stdout
        diagnostics = _parse_ivy_check_output(raw_output)

        return json.dumps({
            "success": result.returncode == 0,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "raw_output": raw_output.strip(),
            "duration_seconds": round(duration, 2),
        })

    @mcp.tool()
    def ivy_compile(
        relative_path: str,
        target: str = "test",
        isolate: str | None = None,
    ) -> str:
        """Compile an Ivy file to a test executable using ivyc.

        When a Docker image is configured (--docker-image), compilation runs
        inside a Docker container with all required C++ dependencies.
        Otherwise falls back to native subprocess execution.

        Args:
            relative_path: Relative path to the .ivy file to compile.
            target: Compilation target (default: "test").
            isolate: Optional isolate name to compile in isolation.
        """
        import subprocess

        try:
            abs_path = _validate_path(root, relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        try:
            _validate_ivy_param(target)
            if isolate:
                _validate_ivy_param(isolate)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})

        # Try API executor path (Docker-aware compilation)
        if executor is not None and base_path is not None:
            try:
                from pathlib import Path as P

                from panther_ivy.api.compiler import generate_compile_commands

                compile_result = generate_compile_commands(
                    ivy_file=P(relative_path),
                    base_path=base_path,
                )

                start = time.monotonic()
                # Run setup commands
                executor.execute(
                    compile_result.setup_commands,
                    workspace_root=root,
                    timeout=30,
                )
                # Run compilation commands
                exec_result = executor.execute(
                    compile_result.compile_commands,
                    workspace_root=root,
                    timeout=300,
                )
                duration = time.monotonic() - start

                return json.dumps({
                    "success": exec_result.exit_code == 0,
                    "output": (
                        exec_result.stderr + "\n" + exec_result.stdout
                    ).strip(),
                    "target": exec_result.target,
                    "duration_seconds": round(duration, 2),
                })
            except ImportError:
                logger.debug(
                    "panther_ivy.api not available; falling back to direct subprocess"
                )
            except Exception as exc:
                logger.warning(
                    "API executor failed: %s; falling back to direct subprocess",
                    exc,
                )

        # Direct subprocess fallback
        cmd = ["ivyc", f"target={target}"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivyc not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 300s"})

        return json.dumps({
            "success": result.returncode == 0,
            "output": (result.stderr + "\n" + result.stdout).strip(),
            "target": "host",
            "duration_seconds": round(time.monotonic() - start, 2),
        })

    @mcp.tool()
    def ivy_model_info(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Display the structure of an Ivy model using ivy_show.

        Args:
            relative_path: Relative path to the .ivy file to inspect.
            isolate: Optional isolate name for a specific isolate.
        """
        import subprocess

        try:
            abs_path = _validate_path(root, relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        if isolate:
            try:
                _validate_ivy_param(isolate)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        cmd = ["ivy_show"]
        if isolate:
            cmd.append(f"isolate={isolate}")
        cmd.append(abs_path)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=root,
            )
        except FileNotFoundError:
            return json.dumps({"success": False, "message": "ivy_show not found on PATH"})
        except subprocess.TimeoutExpired:
            return json.dumps({"success": False, "message": "Timed out after 30s"})

        return json.dumps({
            "success": result.returncode == 0,
            "output": (result.stderr + "\n" + result.stdout).strip(),
            "duration_seconds": round(time.monotonic() - start, 2),
        })

    @mcp.tool()
    def ivy_lint(relative_path: str) -> str:
        """Fast structural lint of an Ivy file (milliseconds, no subprocess).

        Checks: missing #lang header, unmatched braces, unresolved includes.

        Args:
            relative_path: Relative path to the .ivy file to lint.
        """
        try:
            abs_path = _validate_path(root, relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        diagnostics = _check_structural_issues(source, abs_path)
        return json.dumps({
            "file": relative_path,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
            "warning_count": sum(1 for d in diagnostics if d["severity"] == "warning"),
        })

    @mcp.tool()
    def ivy_include_graph(relative_path: str | None = None) -> str:
        """Return the include dependency graph for Ivy files.

        If a file is given, returns its includes and files that include it.
        If omitted, returns the full project include graph.

        Args:
            relative_path: Optional .ivy file to focus on.
        """
        graph: dict[str, list[str]] = {}
        file_by_basename: dict[str, str] = {}

        for rel_path in _find_ivy_files(root):
            basename = os.path.basename(rel_path)[:-4]
            file_by_basename[basename] = rel_path
            try:
                with open(os.path.join(root, rel_path), encoding="utf-8", errors="replace") as f:
                    source = f.read()
                graph[rel_path] = re.findall(r"^include\s+(\w+)", source, re.MULTILINE)
            except OSError:
                continue

        if relative_path is not None:
            includes = graph.get(relative_path, [])
            resolved = [{"module": inc, "resolved_path": file_by_basename.get(inc)} for inc in includes]

            target_basename = os.path.basename(relative_path)
            if target_basename.endswith(".ivy"):
                target_basename = target_basename[:-4]
            included_by = [fp for fp, incs in graph.items() if target_basename in incs]

            # Transitive includes
            transitive: set[str] = set()
            stack = list(includes)
            while stack:
                mod = stack.pop()
                if mod in transitive:
                    continue
                transitive.add(mod)
                mod_path = file_by_basename.get(mod)
                if mod_path and mod_path in graph:
                    stack.extend(graph[mod_path])

            return json.dumps({
                "file": relative_path,
                "includes": resolved,
                "included_by": included_by,
                "transitive_includes": sorted(transitive),
            })
        else:
            return json.dumps({
                "files": {fp: {"includes": incs} for fp, incs in graph.items()},
                "total_files": len(graph),
            })

    @mcp.tool()
    def ivy_capabilities() -> str:
        """Report which Ivy CLI tools are available on PATH."""
        return json.dumps({
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
        })

    # --- Semantic / Traceability Tools ---

    def _get_model():
        """Return the semantic model, building one if needed."""
        nonlocal semantic_model
        if semantic_model is not None:
            return semantic_model

        with _model_lock:
            # Double-check after acquiring lock
            if semantic_model is not None:
                return semantic_model

            return _build_model()

    def _build_model():
        """Build a lightweight semantic model from workspace files."""
        nonlocal semantic_model
        try:
            from ivy_lsp.semantic.model import SemanticModel
            from ivy_lsp.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
                parse_file_rfc_annotations,
            )

            model = SemanticModel()
            # Load manifests
            for manifest_path in find_manifests(root):
                reqs = load_requirement_manifest(manifest_path)
                for req in reqs.values():
                    model.add_node(req)

            # Scan for RFC annotations in .ivy files
            for rel_path in _find_ivy_files(root):
                abs_path = os.path.join(root, rel_path)
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                    for ann in parse_file_rfc_annotations(source, abs_path):
                        model.add_node(ann)
                except OSError:
                    continue

            semantic_model = model
            return model
        except ImportError:
            return None

    @mcp.tool()
    def ivy_traceability_matrix(relative_path: str | None = None) -> str:
        """RFC requirement-to-annotation traceability matrix.

        Shows which RFC requirements are covered by bracket-tag annotations in the codebase.

        Args:
            relative_path: Optional file to scope the matrix to.
        """
        model = _get_model()
        if model is None:
            return json.dumps({"success": False, "message": "Semantic model unavailable"})

        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        if relative_path:
            try:
                abs_path = _validate_path(root, relative_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
            annotations = [a for a in annotations if a.file == abs_path]

        covered_tags: dict[str, list[dict]] = {}
        for ann in annotations:
            for tag in ann.tags:
                if tag not in covered_tags:
                    covered_tags[tag] = []
                covered_tags[tag].append({
                    "file": ann.file,
                    "line": ann.line,
                })

        matrix = []
        for req in requirements:
            matrix.append({
                "id": req.id,
                "rfc": req.rfc,
                "section": req.section,
                "level": req.level,
                "text": req.text[:120],
                "covered": req.id in covered_tags,
                "assertions": covered_tags.get(req.id, []),
            })

        return json.dumps({
            "total_requirements": len(requirements),
            "covered": sum(1 for m in matrix if m["covered"]),
            "uncovered": sum(1 for m in matrix if not m["covered"]),
            "matrix": matrix,
        })

    @mcp.tool()
    def ivy_requirement_coverage(relative_path: str | None = None) -> str:
        """RFC requirement coverage statistics by level (MUST/SHOULD/MAY) and layer.

        Args:
            relative_path: Optional file to scope the analysis to.
        """
        model = _get_model()
        if model is None:
            return json.dumps({"success": False, "message": "Semantic model unavailable"})

        from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

        requirements = model.get_nodes_by_type(RfcRequirement)
        annotations = model.get_nodes_by_type(RfcAnnotation)

        if relative_path:
            try:
                abs_path = _validate_path(root, relative_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
            annotations = [a for a in annotations if a.file == abs_path]

        covered_tags = set()
        for ann in annotations:
            covered_tags.update(ann.tags)

        by_level: dict[str, dict] = {}
        by_layer: dict[str, dict] = {}
        for req in requirements:
            level = req.level or "UNKNOWN"
            layer = getattr(req, "layer", None) or "unspecified"

            if level not in by_level:
                by_level[level] = {"total": 0, "covered": 0}
            by_level[level]["total"] += 1
            if req.id in covered_tags:
                by_level[level]["covered"] += 1

            if layer not in by_layer:
                by_layer[layer] = {"total": 0, "covered": 0}
            by_layer[layer]["total"] += 1
            if req.id in covered_tags:
                by_layer[layer]["covered"] += 1

        total = len(requirements)
        covered = sum(1 for r in requirements if r.id in covered_tags)
        return json.dumps({
            "total": total,
            "covered": covered,
            "uncovered": total - covered,
            "coverage_percent": round(100 * covered / total, 1) if total else 0,
            "by_level": by_level,
            "by_layer": by_layer,
        })

    @mcp.tool()
    def ivy_impact_analysis(symbol_name: str) -> str:
        """Analyze incoming and outgoing edges for a symbol in the semantic model.

        Args:
            symbol_name: The name of the symbol to analyze.
        """
        model = _get_model()
        if model is None:
            return json.dumps({"success": False, "message": "Semantic model unavailable"})

        from ivy_lsp.semantic.nodes import SymbolNode

        # Find matching symbol nodes
        matches = [
            sn for sn in model.get_nodes_by_type(SymbolNode)
            if sn.name == symbol_name or sn.qualified_name == symbol_name
        ]

        if not matches:
            return json.dumps({
                "symbol": symbol_name,
                "found": False,
                "message": f"Symbol '{symbol_name}' not found in semantic model",
            })

        sn = matches[0]
        incoming = model.get_incoming(sn.id)
        outgoing = model.get_outgoing(sn.id)

        return json.dumps({
            "symbol": symbol_name,
            "found": True,
            "qualified_name": sn.qualified_name,
            "kind": sn.kind,
            "file": sn.file,
            "line": sn.line,
            "incoming_edges": [
                {"type": etype.value, "source": src} for etype, src in incoming
            ],
            "outgoing_edges": [
                {"type": etype.value, "target": tgt} for etype, tgt in outgoing
            ],
            "total_references": len(incoming) + len(outgoing),
        })

    @mcp.tool()
    def ivy_extract_requirements(rfc_text: str) -> str:
        """Parse RFC text to extract MUST/SHOULD/MAY structured requirements.

        Args:
            rfc_text: Raw RFC text to parse for normative requirements.
        """
        req_pattern = re.compile(
            r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
            r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
            re.MULTILINE,
        )

        results = []
        for m in req_pattern.finditer(rfc_text):
            text = m.group(1).strip()
            level = m.group(2)
            # Normalize level
            if level in ("SHALL", "REQUIRED"):
                level = "MUST"
            elif level in ("SHALL NOT",):
                level = "MUST NOT"
            elif level in ("RECOMMENDED",):
                level = "SHOULD"
            elif level in ("OPTIONAL",):
                level = "MAY"

            results.append({
                "text": text,
                "level": level,
                "offset": m.start(),
            })

        return json.dumps({
            "requirements": results,
            "total": len(results),
            "by_level": {
                level: sum(1 for r in results if r["level"] == level)
                for level in sorted({r["level"] for r in results})
            },
        })

    @mcp.tool()
    def ivy_cross_references(node_id: str) -> str:
        """Query cross-reference graph neighborhood of a node.

        Args:
            node_id: The node ID to query (e.g., "test.ivy:5:send").
        """
        model = _get_model()
        if model is None:
            return json.dumps({"success": False, "message": "Semantic model unavailable"})

        node = model.get_node(node_id)
        if node is None:
            return json.dumps({
                "node_id": node_id,
                "found": False,
                "message": f"Node '{node_id}' not found",
            })

        incoming = model.get_incoming(node_id)
        outgoing = model.get_outgoing(node_id)

        return json.dumps({
            "node_id": node_id,
            "found": True,
            "node_type": type(node).__name__,
            "incoming": [
                {"type": etype.value, "source": src} for etype, src in incoming
            ],
            "outgoing": [
                {"type": etype.value, "target": tgt} for etype, tgt in outgoing
            ],
        })

    @mcp.tool()
    def ivy_query_symbol(symbol_name: str) -> str:
        """Query rich semantic info about a symbol: type, references, requirements.

        Args:
            symbol_name: The symbol name to query.
        """
        model = _get_model()
        if model is None:
            return json.dumps({"success": False, "message": "Semantic model unavailable"})

        from ivy_lsp.semantic.nodes import SymbolNode, TypeNode

        # Search SymbolNode
        symbol_matches = [
            sn for sn in model.get_nodes_by_type(SymbolNode)
            if sn.name == symbol_name or sn.qualified_name == symbol_name
        ]
        # Search TypeNode
        type_matches = [
            tn for tn in model.get_nodes_by_type(TypeNode)
            if tn.name == symbol_name or tn.qualified_name == symbol_name
        ]

        if not symbol_matches and not type_matches:
            return json.dumps({
                "symbol": symbol_name,
                "found": False,
                "message": f"Symbol '{symbol_name}' not found",
            })

        result: dict[str, Any] = {
            "symbol": symbol_name,
            "found": True,
        }

        if symbol_matches:
            sn = symbol_matches[0]
            result["symbol_info"] = {
                "qualified_name": sn.qualified_name,
                "kind": sn.kind,
                "file": sn.file,
                "line": sn.line,
                "params": sn.params,
                "return_sort": sn.return_sort,
                "sort_name": sn.sort_name,
            }
            incoming = model.get_incoming(sn.id)
            outgoing = model.get_outgoing(sn.id)
            result["references"] = {
                "incoming": len(incoming),
                "outgoing": len(outgoing),
            }

        if type_matches:
            tn = type_matches[0]
            result["type_info"] = {
                "qualified_name": tn.qualified_name,
                "file": tn.file,
                "line": tn.line,
                "sort_name": tn.sort_name,
                "is_enum": tn.is_enum,
                "variants": tn.variants,
            }

        return json.dumps(result)

    # --- Visualization Tools ---

    def _make_viz_server_proxy():
        """Create a minimal server-like object for visualization handlers.

        The visualization handlers in features/visualization.py expect a
        server object with ``server._indexer._requirement_graph``.  This
        builds a lightweight proxy that satisfies that contract.
        """
        indexer = type("_IndexerProxy", (), {
            "_requirement_graph": requirement_graph,
        })()
        return type("_ServerProxy", (), {"_indexer": indexer})()

    @mcp.tool()
    def ivy_action_requirements(
        action_name: str | None = None,
        file_path: str | None = None,
        test_file: str | None = None,
    ) -> str:
        """Get requirements organized by action boundaries (before/after monitors).

        Returns requirements grouped by the action they monitor, their temporal
        position (before/after), kind (require/ensure/assume/assert), and the
        state variables they read or write.

        Args:
            action_name: Specific action to query. If omitted, returns all actions.
            file_path: Scope to actions defined in this file (relative path).
            test_file: Optional test file to scope the analysis to (relative path).
        """
        from ivy_lsp.features.visualization import handle_action_requirements

        server_proxy = _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if action_name:
            params["actionName"] = action_name
        if file_path:
            try:
                params["filePath"] = _validate_path(root, file_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        return json.dumps(handle_action_requirements(server_proxy, params))

    @mcp.tool()
    def ivy_model_summary(test_file: str | None = None) -> str:
        """Get per-action requirement counts, state variable usage, and RFC coverage.

        Returns one row per action with counts of before/after requirements by kind,
        state variables read/written, and RFC bracket tags covered.

        Args:
            test_file: Optional test file to scope the summary to (relative path).
        """
        from ivy_lsp.features.visualization import handle_model_summary_table

        server_proxy = _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        return json.dumps(handle_model_summary_table(server_proxy, params))

    @mcp.tool()
    def ivy_coverage_gaps(test_file: str | None = None) -> str:
        """Identify coverage gaps: unguarded state vars, uncovered RFC requirements.

        Finds state variables written but never guarded, RFC sections with no
        covering assertions, and requirements whose monitored action does not exist.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
        """
        from ivy_lsp.features.visualization import handle_coverage_gaps

        server_proxy = _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        return json.dumps(handle_coverage_gaps(server_proxy, params))

    @mcp.tool()
    def ivy_action_dependency_graph(
        test_file: str | None = None,
        include_state_vars: bool = False,
    ) -> str:
        """Return the action dependency graph showing shared-state relationships.

        Actions are nodes; edges represent shared state variables (action A writes
        a variable that action B reads). Optionally includes state variable nodes
        with explicit reads/writes edges.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
            include_state_vars: When True, include state variable nodes and their
                reads/writes edges in the graph.
        """
        from ivy_lsp.features.visualization import handle_action_dependency_graph

        server_proxy = _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if include_state_vars:
            params["includeStateVars"] = True
        return json.dumps(handle_action_dependency_graph(server_proxy, params))

    @mcp.tool()
    def ivy_state_machine_view(
        test_file: str | None = None,
        state_var_filter: str | None = None,
    ) -> str:
        """Return a state-machine view of the Ivy specification.

        Models the specification as a state machine where state variables are
        state nodes, actions are transitions between them (via READS/WRITES),
        and guards are require/assume clauses on the action's monitors.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
            state_var_filter: Optional state variable name to restrict the view to.
        """
        from ivy_lsp.features.visualization import handle_state_machine_view

        server_proxy = _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if state_var_filter:
            params["stateVarFilter"] = state_var_filter
        return json.dumps(handle_state_machine_view(server_proxy, params))

    if _return_app:
        return mcp

    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    mcp.run(transport="stdio")
