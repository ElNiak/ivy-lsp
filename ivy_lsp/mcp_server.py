"""MCP server mode for ivy-lsp.

Exposes Ivy verification tools, structured diagnostics, include graph,
and fast lint as MCP tools via the Model Context Protocol. Shares the
same parsing and indexing code as the LSP server.

Usage:
    python -m ivy_lsp --mcp
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from typing import Any

from ivy_lsp.utils.ivy_output import extract_error_summary, parse_ivy_output
from ivy_lsp.utils.validation import validate_ivy_param as _validate_ivy_param
from ivy_lsp.verification import (
    run_ivy_check as shared_ivy_check,
    run_ivy_compile as shared_ivy_compile,
    run_ivy_show as shared_ivy_show,
)

logger = logging.getLogger(__name__)

# Pre-compiled regex patterns for hot-path performance
_INCLUDE_PATTERN = re.compile(r"^include\s+(\w+)", re.MULTILINE)
_RFC_REQ_PATTERN = re.compile(
    r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
    r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
    re.MULTILINE,
)

# Symbol/type extraction patterns for lightweight semantic model
_TYPE_DECL_RE = re.compile(
    r"^\s*type\s+([\w.]+)(?:\s*=\s*\{([^}]+)\})?", re.MULTILINE
)
_ACTION_DECL_RE = re.compile(
    r"^\s*action\s+([\w.]+)\s*(?:\(([^)]*)\))?(?:\s*returns\s*\(([^)]*)\))?",
    re.MULTILINE,
)
_RELATION_DECL_RE = re.compile(
    r"^\s*relation\s+([\w.]+)\s*(?:\(([^)]*)\))?", re.MULTILINE
)
_FUNCTION_DECL_RE = re.compile(
    r"^\s*function\s+([\w.]+)\s*(?:\(([^)]*)\))?(?:\s*:\s*(\w+))?",
    re.MULTILINE,
)
_INDIVIDUAL_DECL_RE = re.compile(
    r"^\s*individual\s+([\w.]+)\s*:\s*(\w+)", re.MULTILINE
)
_OBJECT_DECL_RE = re.compile(
    r"^\s*(?:object|module|isolate)\s+([\w.]+)\s*(?:=\s*\{)?", re.MULTILINE
)


def _validate_path(root: str, relative_path: str) -> str:
    """Resolve *relative_path* under *root*, rejecting traversal escapes."""
    abs_path = os.path.realpath(os.path.join(root, relative_path))
    real_root = os.path.realpath(root)
    if not abs_path.startswith(real_root + os.sep) and abs_path != real_root:
        raise ValueError(f"Path escapes workspace root: {relative_path}")
    return abs_path


from ivy_lsp.utils.ivy_output import find_ivy_files as _find_ivy_files
from ivy_lsp.utils.structural_lint import (
    check_structural_issues_raw,
    check_unresolved_includes_raw,
)


def _check_structural_issues(
    source: str,
    filepath: str,
    resolve_callback: Any = None,
) -> list[dict[str, Any]]:
    """Fast structural checks without full parsing."""
    diags = check_structural_issues_raw(source, filepath)
    diags.extend(check_unresolved_includes_raw(source, filepath, resolve_callback))
    return diags


def start_mcp(
    workspace_root: str | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
    docker_image: str | None = None,
    base_path: str | None = None,
    staging_dir: str | None = None,
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
        staging_dir: Optional staging directory for include resolution.
            When set, ivy_verify/ivy_compile/ivy_model_info resolve paths
            through this directory (flat symlinks for CWD-relative includes).
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
            from api.executor import IvyExecutor

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

    _model_lock = asyncio.Lock()
    _model_build_attempted = False

    _req_graph_lock = asyncio.Lock()
    _req_graph: Any = requirement_graph  # may be pre-populated or None
    _req_graph_build_attempted = False

    # --- Workspace-aware include resolution cache ---

    # Known Ivy standard library modules that should never be flagged
    # as unresolved by lint (they live in ivy/include/ or ivy/ivy2/).
    _STDLIB_MODULES = frozenset({
        "order", "collections", "ip", "ipv6", "tcp", "udp",
        "byte_stream", "timeout", "net",
    })

    _basename_cache: dict[str, list[str]] | None = None

    def _get_basename_cache() -> dict[str, list[str]]:
        """Build (or return cached) basename→[relative_paths] map for all .ivy files."""
        nonlocal _basename_cache
        if _basename_cache is not None:
            return _basename_cache

        cache: dict[str, list[str]] = {}
        for rel_path in _find_ivy_files(root):
            basename = os.path.basename(rel_path)[:-4]  # strip .ivy
            cache.setdefault(basename, []).append(rel_path)
        # Also index stdlib files that _find_ivy_files may discover
        _basename_cache = cache
        return cache

    def _make_resolve_callback():
        """Create a resolve callback for structural lint include checking."""
        cache = _get_basename_cache()

        def _resolve(inc_name: str, from_file: str) -> str | None:
            # Accept known stdlib modules
            if inc_name in _STDLIB_MODULES:
                return f"<stdlib>/{inc_name}.ivy"
            # Check workspace file index
            candidates = cache.get(inc_name)
            if candidates:
                return candidates[0]  # first match is sufficient for lint
            return None

        return _resolve

    @mcp.tool()
    async def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Run ivy_check on an Ivy file to verify formal properties.

        Returns structured diagnostics with file, line, severity, and message.

        Args:
            relative_path: Relative path to the .ivy file to check.
            isolate: Optional isolate name to check in isolation.
        """
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

        result = await shared_ivy_check(
            filepath=abs_path,
            workspace_root=root,
            isolate=isolate,
            staging_dir=staging_dir,
        )
        return json.dumps(result)

    @mcp.tool()
    async def ivy_compile(
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

                from api.compiler import generate_compile_commands

                compile_result = generate_compile_commands(
                    ivy_file=P(relative_path),
                    base_path=base_path,
                )

                start = time.monotonic()
                # Run setup + compilation via thread pool (executor.execute is blocking)
                await asyncio.to_thread(
                    executor.execute,
                    compile_result.setup_commands,
                    workspace_root=root,
                    timeout=30,
                )
                exec_result = await asyncio.to_thread(
                    executor.execute,
                    compile_result.compile_commands,
                    workspace_root=root,
                    timeout=300,
                )
                duration = time.monotonic() - start

                raw_output = (
                    exec_result.stderr + "\n" + exec_result.stdout
                ).strip()
                diagnostics = parse_ivy_output(raw_output)
                return json.dumps({
                    "success": exec_result.exit_code == 0
                    and not any(
                        d["severity"] == "error" for d in diagnostics
                    ),
                    "diagnostics": diagnostics,
                    "diagnostic_count": len(diagnostics),
                    "error_summary": extract_error_summary(
                        raw_output, diagnostics
                    ),
                    "raw_output": raw_output,
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
        result = await shared_ivy_compile(
            filepath=abs_path,
            workspace_root=root,
            target=target,
            isolate=isolate,
            staging_dir=staging_dir,
        )
        return json.dumps(result)

    @mcp.tool()
    async def ivy_model_info(
        relative_path: str,
        isolate: str | None = None,
    ) -> str:
        """Display the structure of an Ivy model using ivy_show.

        Args:
            relative_path: Relative path to the .ivy file to inspect.
            isolate: Optional isolate name for a specific isolate.
        """
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

        result = await shared_ivy_show(
            filepath=abs_path,
            workspace_root=root,
            isolate=isolate,
            staging_dir=staging_dir,
        )
        return json.dumps(result)

    @mcp.tool()
    async def ivy_lint(relative_path: str) -> str:
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

        resolve_cb = _make_resolve_callback()
        diagnostics = _check_structural_issues(source, abs_path, resolve_cb)
        return json.dumps({
            "success": True,
            "file": relative_path,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
            "warning_count": sum(1 for d in diagnostics if d["severity"] == "warning"),
        })

    @mcp.tool()
    async def ivy_include_graph(relative_path: str | None = None) -> str:
        """Return the include dependency graph for Ivy files.

        If a file is given, returns its includes and files that include it.
        If omitted, returns the full project include graph.

        Args:
            relative_path: Optional .ivy file to focus on.
        """

        def _build_graph():
            graph: dict[str, list[str]] = {}
            # Use shared basename cache (list-based, no collisions)
            cache = _get_basename_cache()

            for rel_path in _find_ivy_files(root):
                try:
                    with open(os.path.join(root, rel_path), encoding="utf-8", errors="replace") as f:
                        source = f.read()
                    graph[rel_path] = _INCLUDE_PATTERN.findall(source)
                except OSError:
                    continue

            return graph, cache

        def _resolve_closest(
            inc_name: str,
            from_file: str,
            cache: dict[str, list[str]],
        ) -> str | None:
            """Resolve include to the closest matching file by path proximity."""
            candidates = cache.get(inc_name)
            if not candidates:
                return None
            if len(candidates) == 1:
                return candidates[0]
            # Prefer the file sharing the longest common path prefix
            from_dir = os.path.dirname(from_file)
            best = candidates[0]
            best_len = 0
            for c in candidates:
                prefix = os.path.commonpath([from_dir, os.path.dirname(c)]) if from_dir else ""
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best = c
            return best

        graph, basename_cache = await asyncio.to_thread(_build_graph)

        if relative_path is not None:
            includes = graph.get(relative_path, [])
            resolved = []
            for inc in includes:
                rp = _resolve_closest(inc, relative_path, basename_cache)
                entry: dict[str, Any] = {"module": inc, "resolved_path": rp}
                # Flag ambiguous resolution
                candidates = basename_cache.get(inc, [])
                if len(candidates) > 1:
                    entry["candidates"] = candidates
                resolved.append(entry)

            target_basename = os.path.basename(relative_path)
            if target_basename.endswith(".ivy"):
                target_basename = target_basename[:-4]
            included_by = [fp for fp, incs in graph.items() if target_basename in incs]

            # Transitive includes (using proximity-based resolution)
            transitive: set[str] = set()
            stack = list(includes)
            while stack:
                mod = stack.pop()
                if mod in transitive:
                    continue
                transitive.add(mod)
                mod_path = _resolve_closest(mod, relative_path, basename_cache)
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
    async def ivy_capabilities() -> str:
        """Report which Ivy CLI tools are available on PATH."""
        return json.dumps({
            "success": True,
            "ivy_check": shutil.which("ivy_check") is not None,
            "ivyc": shutil.which("ivyc") is not None,
            "ivy_show": shutil.which("ivy_show") is not None,
        })

    # --- Semantic / Traceability Tools ---

    async def _get_model():
        """Return the semantic model, building one if needed."""
        nonlocal semantic_model, _model_build_attempted
        # Fast path: model already built
        if semantic_model is not None:
            return semantic_model
        # Fast path: previous build failed (cached failure)
        if _model_build_attempted:
            return None

        async with _model_lock:
            # Double-check after acquiring lock
            if semantic_model is not None:
                return semantic_model
            if _model_build_attempted:
                return None

            model = await asyncio.to_thread(_build_model)
            if model is not None:
                semantic_model = model
            else:
                _model_build_attempted = True
            return model

    def _build_model():
        """Build a lightweight semantic model from workspace files.

        Returns the model on success, or ``None`` when a required
        dependency is missing (logged at WARNING).  The caller
        (``_get_model``) is responsible for caching the result and
        assigning the ``semantic_model`` nonlocal under the lock.
        """
        try:
            from ivy_lsp.semantic.model import SemanticModel
            from ivy_lsp.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
                parse_file_rfc_annotations,
            )

            from ivy_lsp.semantic.nodes import SymbolNode, TypeNode

            model = SemanticModel()
            # Load manifests
            for manifest_path in find_manifests(root):
                reqs = load_requirement_manifest(manifest_path)
                for req in reqs.values():
                    model.add_node(req)

            # Scan .ivy files for annotations, types, and symbols
            for rel_path in _find_ivy_files(root):
                abs_path = os.path.join(root, rel_path)
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue

                # RFC annotations
                for ann in parse_file_rfc_annotations(source, abs_path):
                    model.add_node(ann)

                # Type declarations
                for m in _TYPE_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    variants_raw = m.group(2)
                    is_enum = variants_raw is not None
                    variants = (
                        [v.strip() for v in variants_raw.split(",") if v.strip()]
                        if variants_raw
                        else []
                    )
                    model.add_node(TypeNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        file=abs_path,
                        line=line,
                        is_enum=is_enum,
                        variants=variants,
                    ))

                # Action declarations
                for m in _ACTION_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    params = (
                        [p.strip() for p in m.group(2).split(",") if p.strip()]
                        if m.group(2)
                        else []
                    )
                    ret = m.group(3).strip() if m.group(3) else None
                    model.add_node(SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="action",
                        file=abs_path,
                        line=line,
                        params=params,
                        return_sort=ret,
                    ))

                # Relation declarations
                for m in _RELATION_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    model.add_node(SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="relation",
                        file=abs_path,
                        line=line,
                    ))

                # Function declarations
                for m in _FUNCTION_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    ret_sort = m.group(3) if m.group(3) else None
                    model.add_node(SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="function",
                        file=abs_path,
                        line=line,
                        return_sort=ret_sort,
                    ))

                # Individual declarations
                for m in _INDIVIDUAL_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    sort_name = m.group(2)
                    model.add_node(SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="individual",
                        file=abs_path,
                        line=line,
                        sort_name=sort_name,
                    ))

                # Object/module/isolate declarations
                for m in _OBJECT_DECL_RE.finditer(source):
                    name = m.group(1)
                    line = source[: m.start()].count("\n")
                    model.add_node(SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="module",
                        file=abs_path,
                        line=line,
                    ))

            return model
        except ImportError:
            logger.warning(
                "Semantic model unavailable: required modules "
                "(ivy_lsp.semantic.model or ivy_lsp.semantic.rfc_annotations) "
                "could not be imported. Install ivy-lsp[semantic] to enable "
                "traceability tools.",
                exc_info=True,
            )
            return None

    # --- Lazy RequirementGraph construction ---

    async def _get_req_graph():
        """Return the requirement graph, lazily building one if needed."""
        nonlocal _req_graph, _req_graph_build_attempted
        if _req_graph is not None:
            return _req_graph
        if _req_graph_build_attempted:
            return None

        async with _req_graph_lock:
            if _req_graph is not None:
                return _req_graph
            if _req_graph_build_attempted:
                return None

            graph = await asyncio.to_thread(_build_requirement_graph)
            if graph is not None:
                _req_graph = graph
            else:
                _req_graph_build_attempted = True
            return graph

    def _build_requirement_graph():
        """Build a RequirementGraph from workspace .ivy files.

        Uses the light-mode extractor (regex or PLY lexer) to extract
        requirements and writes from each file, populates ActionNode
        and RequirementNode entries, wires CONSTRAINS and WRITES edges.
        """
        try:
            from ivy_lsp.analysis.light_mode_extractor import (
                extract_requirements_light,
            )
            from ivy_lsp.analysis.requirement_graph import (
                ActionNode,
                RequirementGraph,
                StateVarNode,
            )

            graph = RequirementGraph()
            all_writes: list[tuple[str, str, int]] = []
            known_vars: set[str] = set()

            for rel_path in _find_ivy_files(root):
                abs_path = os.path.join(root, rel_path)
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError:
                    continue

                reqs, writes = extract_requirements_light(source, abs_path)
                if not reqs and not writes:
                    continue

                # Bulk-add requirements + CONSTRAINS edges for this file
                graph.add_file_requirements(abs_path, reqs, writes)

                # Collect write targets as known state vars
                for var_name, _fp, _line in writes:
                    known_vars.add(var_name)
                all_writes.extend(writes)

            # Create ActionNodes from monitor_action references
            for req in graph.requirements.values():
                if req.monitor_action:
                    graph.add_action_if_absent(ActionNode(
                        id=req.monitor_action,
                        name=req.monitor_action.rsplit(".", 1)[-1],
                        qualified_name=req.monitor_action,
                        file=req.file,
                        line=req.line,
                    ))

            # Create StateVarNodes from write targets
            for var_name, filepath_w, line_w in all_writes:
                if var_name not in graph.state_vars:
                    graph.add_state_var(StateVarNode(
                        id=var_name,
                        name=var_name.rsplit(".", 1)[-1],
                        qualified_name=var_name,
                        file=filepath_w,
                        line=line_w,
                    ))

            # Wire READS edges from requirements to state vars
            if known_vars:
                try:
                    graph.wire_state_var_edges(known_vars)
                except ImportError:
                    logger.debug(
                        "formula_analyzer unavailable; skipping READS edge wiring"
                    )

            # Load RFC requirement manifests and wire COVERS edges
            try:
                from ivy_lsp.semantic.rfc_annotations import (
                    find_manifests,
                    load_requirement_manifest,
                )

                for manifest_path in find_manifests(root):
                    reqs_dict = load_requirement_manifest(manifest_path)
                    for rfc_req in reqs_dict.values():
                        graph.add_rfc_requirement(rfc_req)

                if graph.rfc_requirements:
                    graph.wire_coverage_edges()
            except ImportError:
                logger.debug("rfc_annotations unavailable; skipping manifest loading")

            total = (
                len(graph.requirements)
                + len(graph.actions)
                + len(graph.state_vars)
            )
            logger.info(
                "Built requirement graph: %d requirements, %d actions, "
                "%d state vars, %d edges",
                len(graph.requirements),
                len(graph.actions),
                len(graph.state_vars),
                len(graph.edges),
            )
            return graph if total > 0 else None
        except Exception:
            logger.warning(
                "Failed to build requirement graph",
                exc_info=True,
            )
            return None

    @mcp.tool()
    async def ivy_traceability_matrix(relative_path: str | None = None) -> str:
        """RFC requirement-to-annotation traceability matrix.

        Shows which RFC requirements are covered by bracket-tag annotations in the codebase.

        Args:
            relative_path: Optional file to scope the matrix to.
        """
        model = await _get_model()
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
    async def ivy_requirement_coverage(relative_path: str | None = None) -> str:
        """RFC requirement coverage statistics by level (MUST/SHOULD/MAY) and layer.

        Args:
            relative_path: Optional file to scope the analysis to.
        """
        model = await _get_model()
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
    async def ivy_impact_analysis(symbol_name: str) -> str:
        """Analyze incoming and outgoing edges for a symbol in the semantic model.

        Args:
            symbol_name: The name of the symbol to analyze.
        """
        model = await _get_model()
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
    async def ivy_extract_requirements(rfc_text: str) -> str:
        """Parse RFC text to extract MUST/SHOULD/MAY structured requirements.

        Args:
            rfc_text: Raw RFC text to parse for normative requirements.
        """
        results = []
        for m in _RFC_REQ_PATTERN.finditer(rfc_text):
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
    async def ivy_cross_references(node_id: str) -> str:
        """Query cross-reference graph neighborhood of a node.

        Args:
            node_id: The node ID to query (e.g., "test.ivy:5:send").
        """
        model = await _get_model()
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
    async def ivy_query_symbol(symbol_name: str) -> str:
        """Query rich semantic info about a symbol: type, references, requirements.

        Args:
            symbol_name: The symbol name to query.
        """
        model = await _get_model()
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

    from dataclasses import dataclass

    @dataclass
    class _IndexerProxy:
        requirement_graph: Any

    @dataclass
    class _ServerProxy:
        indexer: _IndexerProxy
        initializing: bool = False

    async def _make_viz_server_proxy():
        """Create a minimal server-like object for visualization handlers.

        The visualization handlers in features/visualization.py expect a
        server object with ``server.indexer.requirement_graph``.  This
        lazily builds the requirement graph on first use and returns a
        lightweight proxy that satisfies that contract.
        """
        graph = await _get_req_graph()
        return _ServerProxy(indexer=_IndexerProxy(requirement_graph=graph))

    @mcp.tool()
    async def ivy_action_requirements(
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

        server_proxy = await _make_viz_server_proxy()
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
    async def ivy_model_summary(test_file: str | None = None) -> str:
        """Get per-action requirement counts, state variable usage, and RFC coverage.

        Returns one row per action with counts of before/after requirements by kind,
        state variables read/written, and RFC bracket tags covered.

        Args:
            test_file: Optional test file to scope the summary to (relative path).
        """
        from ivy_lsp.features.visualization import handle_model_summary_table

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        return json.dumps(handle_model_summary_table(server_proxy, params))

    @mcp.tool()
    async def ivy_coverage_gaps(test_file: str | None = None) -> str:
        """Identify coverage gaps: unguarded state vars, uncovered RFC requirements.

        Finds state variables written but never guarded, RFC sections with no
        covering assertions, and requirements whose monitored action does not exist.

        Args:
            test_file: Optional test file to scope the analysis to (relative path).
        """
        from ivy_lsp.features.visualization import handle_coverage_gaps

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        return json.dumps(handle_coverage_gaps(server_proxy, params))

    @mcp.tool()
    async def ivy_action_dependency_graph(
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

        server_proxy = await _make_viz_server_proxy()
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
    async def ivy_state_machine_view(
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

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if state_var_filter:
            params["stateVarFilter"] = state_var_filter
        return json.dumps(handle_state_machine_view(server_proxy, params))

    @mcp.tool()
    async def ivy_layered_overview(
        test_file: str | None = None,
        group_by: str = "file",
    ) -> str:
        """Get a layered overview of the Ivy model organized by file or module.

        Args:
            test_file: Optional test file to scope the overview to (relative path).
            group_by: Grouping strategy: "file" (default) or "module".
        """
        from ivy_lsp.features.visualization import handle_layered_overview

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if group_by:
            params["groupBy"] = group_by
        return json.dumps(handle_layered_overview(server_proxy, params))

    @mcp.tool()
    async def ivy_smart_suggestions(
        file_path: str | None = None,
        line: int | None = None,
        context: str | None = None,
    ) -> str:
        """Get context-aware suggestions for improving the Ivy specification.

        Args:
            file_path: File to analyze (relative path).
            line: Optional line number for cursor-local suggestions.
            context: Optional context hint (e.g., "monitor", "property").
        """
        from ivy_lsp.features.visualization import handle_smart_suggestions

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if file_path:
            try:
                params["filePath"] = _validate_path(root, file_path)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if line is not None:
            params["line"] = line
        if context:
            params["context"] = context
        return json.dumps(handle_smart_suggestions(server_proxy, params))

    if _return_app:
        return mcp

    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    mcp.run(transport="stdio")
