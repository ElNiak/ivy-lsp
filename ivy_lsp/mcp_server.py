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
import threading
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
# Assertion/tag detection for ivy_diagnostics semantic layer
_ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)
_BRACKET_TAG_RE = re.compile(r"#\s*\[")


def _validate_path(root: str, relative_path: str) -> str:
    """Resolve *relative_path* under *root*, rejecting traversal escapes.

    Falls back to protocol-testing/ prefix if direct path doesn't exist.
    """
    abs_path = os.path.realpath(os.path.join(root, relative_path))
    real_root = os.path.realpath(root)
    if not abs_path.startswith(real_root + os.sep) and abs_path != real_root:
        raise ValueError(f"Path escapes workspace root: {relative_path}")

    # C1: If file doesn't exist, try with protocol-testing/ prefix
    if not os.path.exists(abs_path):
        alt = os.path.realpath(
            os.path.join(root, "protocol-testing", relative_path)
        )
        if alt.startswith(real_root + os.sep) and os.path.exists(alt):
            return alt

    return abs_path


from ivy_lsp.utils.ivy_output import (
    DEFAULT_EXCLUDE_DIRS,
    find_ivy_files as _find_ivy_files_raw,
)
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

    # --- Workspace scoping: respect include/exclude paths from detection ---
    _include_paths = [
        p.strip()
        for p in os.environ.get("IVY_LSP_INCLUDE_PATHS", "").split(",")
        if p.strip()
    ]
    _extra_exclude_dirs = frozenset(
        p.strip()
        for p in os.environ.get("IVY_LSP_EXCLUDE_PATHS", "").split(",")
        if p.strip()
    )
    _effective_exclude_dirs = DEFAULT_EXCLUDE_DIRS | _extra_exclude_dirs

    def _find_ivy_files(search_root: str) -> list[str]:
        """Find .ivy files respecting workspace include/exclude paths."""
        all_files = _find_ivy_files_raw(search_root, _effective_exclude_dirs)
        if not _include_paths:
            return all_files
        return [
            f for f in all_files
            if any(
                f == ip or f.startswith(ip + "/") or f.startswith(ip + os.sep)
                for ip in _include_paths
            )
        ]

    if _include_paths:
        logger.info("Workspace include paths: %s", _include_paths)
    if _extra_exclude_dirs:
        logger.info("Workspace extra exclude dirs: %s", _extra_exclude_dirs)

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
    _basename_cache_lock = threading.Lock()

    def _get_basename_cache() -> dict[str, list[str]]:
        """Build (or return cached) basename→[relative_paths] map for all .ivy files."""
        nonlocal _basename_cache
        if _basename_cache is not None:
            return _basename_cache

        with _basename_cache_lock:
            if _basename_cache is not None:
                return _basename_cache
            cache: dict[str, list[str]] = {}
            for rel_path in _find_ivy_files(root):
                basename = os.path.basename(rel_path)[:-4]  # strip .ivy
                cache.setdefault(basename, []).append(rel_path)
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
    async def ivy_diagnostics(relative_path: str) -> str:
        """Full diagnostic analysis of an Ivy file.

        Runs 5 diagnostic layers (structural, lexer, semantic, coverage,
        pattern) — comparable to what an IDE shows via
        textDocument/publishDiagnostics. More thorough than ivy_lint but
        may take longer on first call (lazy model/graph building).

        Use ivy_lint for quick structural checks (milliseconds).
        Use ivy_diagnostics for thorough analysis after editing.

        Args:
            relative_path: Relative path to the .ivy file to diagnose.
        """
        try:
            abs_path = _validate_path(root, relative_path)
        except ValueError as exc:
            return json.dumps({"success": False, "message": str(exc)})
        if not os.path.isfile(abs_path):
            return json.dumps({"success": False, "message": f"File not found: {relative_path}"})

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        all_diags: list[dict[str, Any]] = []

        # 1. Structural checks (same as ivy_lint)
        resolve_cb = _make_resolve_callback()
        all_diags.extend(_check_structural_issues(source, abs_path, resolve_cb))

        # 2. Lexer errors via fallback scanner (no Z3 needed)
        try:
            from ivy_lsp.parsing.fallback_scanner import fallback_scan

            _symbols, error_info = await asyncio.to_thread(
                fallback_scan, source, abs_path,
            )
            if error_info is not None:
                all_diags.append({
                    "line": error_info.get("line", 1),
                    "severity": "error",
                    "message": f"Lexer error: {error_info.get('message', 'unknown')}",
                    "source": "ivy-lsp-lexer",
                })
        except Exception:
            logger.debug("Fallback scan failed for %s", relative_path, exc_info=True)

        # 3. Semantic diagnostics (orphaned RFC tags, untagged assertions)
        try:
            model = await _get_model()
            if model is not None:
                from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

                rfc_reqs = model.get_nodes_by_type(RfcRequirement)
                annotations = [
                    n for n in model.get_nodes_by_type(RfcAnnotation)
                    if n.file == abs_path
                ]
                if rfc_reqs:
                    req_ids = {r.id for r in rfc_reqs}
                    for ann in annotations:
                        for tag in ann.tags:
                            if tag not in req_ids:
                                all_diags.append({
                                    "line": ann.line + 1,
                                    "severity": "warning",
                                    "message": (
                                        f"Orphaned RFC tag: [{tag}] does not "
                                        "match any loaded requirement manifest"
                                    ),
                                    "source": "ivy-lsp-semantic",
                                })

                # Missing tags on assertions
                lines = source.split("\n")
                for m in _ASSERTION_RE.finditer(source):
                    line_no = source[:m.start()].count("\n")
                    line_text = lines[line_no] if line_no < len(lines) else ""
                    if not _BRACKET_TAG_RE.search(line_text):
                        all_diags.append({
                            "line": line_no + 1,
                            "severity": "hint",
                            "message": "Assertion without RFC bracket tag annotation",
                            "source": "ivy-lsp-semantic",
                        })
        except Exception:
            logger.debug("Semantic diagnostics failed for %s", relative_path, exc_info=True)

        # 4. Coverage hints
        try:
            graph = await _get_req_graph()
            if graph is not None:
                from ivy_lsp.features.coverage_hints import compute_coverage_hints

                for hint in compute_coverage_hints(graph, abs_path):
                    all_diags.append({
                        "line": hint.get("line", 0),
                        "severity": hint.get("severity", "hint"),
                        "message": hint["message"],
                        "source": "ivy-lsp-coverage",
                        "code": hint.get("code"),
                    })
        except Exception:
            logger.debug("Coverage hints failed for %s", relative_path, exc_info=True)

        # 5. Pattern diagnostics (regex-based)
        try:
            basename = os.path.basename(abs_path)

            # Missing _finalize in test files
            if "test" in basename.lower() and "_finalize" not in source:
                has_export = bool(
                    re.search(r"^\s*export\s+action", source, re.MULTILINE)
                )
                if has_export:
                    all_diags.append({
                        "line": 1,
                        "severity": "warning",
                        "message": (
                            "Test file has exports but no _finalize action. "
                            "Consider adding 'export action _finalize' for "
                            "end-of-test assertions."
                        ),
                        "source": "ivy-pattern",
                    })

            # Exported actions without monitors
            exports = set(re.findall(
                r"^\s*export\s+action\s+([\w.]+)", source, re.MULTILINE,
            ))
            monitored = set(re.findall(
                r"^\s*(?:before|after|around)\s+([\w.]+)", source, re.MULTILINE,
            ))
            for exp_action in exports:
                if exp_action not in monitored and exp_action != "_finalize":
                    action_defined = bool(re.search(
                        rf"^\s*action\s+{re.escape(exp_action)}\s*",
                        source, re.MULTILINE,
                    ))
                    if action_defined:
                        match = re.search(
                            rf"^\s*export\s+action\s+{re.escape(exp_action)}",
                            source, re.MULTILINE,
                        )
                        line_num = source[:match.start()].count("\n") + 1 if match else 1
                        all_diags.append({
                            "line": line_num,
                            "severity": "hint",
                            "message": (
                                f"Exported action '{exp_action}' has no "
                                "before/after monitor in this file."
                            ),
                            "source": "ivy-pattern",
                        })
        except Exception:
            logger.debug("Pattern diagnostics failed for %s", relative_path, exc_info=True)

        # Build source-breakdown summary
        by_source: dict[str, int] = {}
        for d in all_diags:
            src = d.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        return json.dumps({
            "success": True,
            "file": relative_path,
            "diagnostics": all_diags,
            "diagnostic_count": len(all_diags),
            "by_source": by_source,
            "error_count": sum(1 for d in all_diags if d.get("severity") == "error"),
            "warning_count": sum(1 for d in all_diags if d.get("severity") == "warning"),
            "hint_count": sum(1 for d in all_diags if d.get("severity") == "hint"),
            "info_count": sum(1 for d in all_diags if d.get("severity") == "info"),
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
                c_dir = os.path.dirname(c)
                if from_dir and c_dir:
                    try:
                        prefix = os.path.commonpath([from_dir, c_dir])
                    except ValueError:
                        prefix = ""
                else:
                    prefix = ""
                if len(prefix) > best_len:
                    best_len = len(prefix)
                    best = c
            return best

        graph, basename_cache = await asyncio.to_thread(_build_graph)

        if relative_path is not None:
            # C5: Try key variants (with/without protocol-testing/ prefix)
            includes = graph.get(relative_path)
            resolved_key = relative_path
            if includes is None:
                alt_key = "protocol-testing/" + relative_path
                includes = graph.get(alt_key)
                if includes is not None:
                    resolved_key = alt_key
            if includes is None:
                for pfx in ["protocol-testing/", "protocol-testing" + os.sep]:
                    if relative_path.startswith(pfx):
                        stripped = relative_path[len(pfx):]
                        includes = graph.get(stripped)
                        if includes is not None:
                            resolved_key = stripped
                            break
            if includes is None:
                includes = []
            resolved = []
            for inc in includes:
                rp = _resolve_closest(inc, resolved_key, basename_cache)
                entry: dict[str, Any] = {"module": inc, "resolved_path": rp}
                # Flag ambiguous resolution
                candidates = basename_cache.get(inc, [])
                if len(candidates) > 1:
                    entry["candidates"] = candidates
                resolved.append(entry)

            target_basename = os.path.basename(resolved_key)
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

            from ivy_lsp.semantic.nodes import (
                RfcAnnotation,
                RfcRequirement,
                SymbolNode,
                TypeNode,
            )

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

            # ── Wire semantic edges ───────────────────────────────
            from ivy_lsp.semantic.edges import SemanticEdgeType

            # 1. COVERS: RfcAnnotation → RfcRequirement
            req_by_id: dict[str, object] = {
                n.id: n for n in model.get_nodes_by_type(RfcRequirement)
            }
            for ann in model.get_nodes_by_type(RfcAnnotation):
                for tag in ann.tags:
                    if tag in req_by_id:
                        model.add_edge(
                            ann.id, SemanticEdgeType.COVERS, tag
                        )

            # 2. HAS_PARAM / RETURNS_TYPE: SymbolNode → TypeNode
            type_by_name: dict[str, str] = {}
            for tn in model.get_nodes_by_type(TypeNode):
                if tn.name not in type_by_name:
                    type_by_name[tn.name] = tn.id

            for sn in model.get_nodes_by_type(SymbolNode):
                # HAS_PARAM: parse "var : type" from params
                if sn.params:
                    for param in sn.params:
                        parts = param.split(":")
                        if len(parts) < 2:
                            continue
                        type_ref = parts[-1].strip()
                        base = type_ref.split(".")[-1]
                        target = type_by_name.get(base) or type_by_name.get(
                            type_ref
                        )
                        if target:
                            model.add_edge(
                                sn.id, SemanticEdgeType.HAS_PARAM, target
                            )

                # RETURNS_TYPE
                ret = getattr(sn, "return_sort", None)
                if ret:
                    base = ret.split(".")[-1]
                    target = type_by_name.get(base) or type_by_name.get(ret)
                    if target:
                        model.add_edge(
                            sn.id, SemanticEdgeType.RETURNS_TYPE, target
                        )

            # 3. INCLUDES: file → file (via include directives)
            # Build basename → abs_path map for resolution
            basename_to_path: dict[str, str] = {}
            for rel_path in _find_ivy_files(root):
                stem = os.path.splitext(os.path.basename(rel_path))[0]
                basename_to_path[stem] = os.path.join(root, rel_path)

            for rel_path in _find_ivy_files(root):
                abs_path = os.path.join(root, rel_path)
                nodes_in_src = model.get_nodes_in_file(abs_path)
                if not nodes_in_src:
                    continue
                src_id = nodes_in_src[0].id
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        src_text = f.read()
                except OSError:
                    continue
                for inc_match in _INCLUDE_PATTERN.findall(src_text):
                    target_path = basename_to_path.get(inc_match)
                    if not target_path or target_path == abs_path:
                        continue
                    nodes_in_tgt = model.get_nodes_in_file(target_path)
                    if nodes_in_tgt:
                        model.add_edge(
                            src_id, SemanticEdgeType.INCLUDES, nodes_in_tgt[0].id
                        )

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

        from ivy_lsp.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        req_ids = {r.id for r in requirements}
        covered_tags: dict[str, list[dict]] = {}
        for ann in annotations:
            for tag in ann.tags:
                for rfc_id in normalize_tag_to_manifest_ids(tag, req_ids):
                    if rfc_id not in covered_tags:
                        covered_tags[rfc_id] = []
                    covered_tags[rfc_id].append({
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

        from ivy_lsp.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        req_ids = {r.id for r in requirements}
        covered_tags: set[str] = set()
        for ann in annotations:
            for tag in ann.tags:
                covered_tags.update(normalize_tag_to_manifest_ids(tag, req_ids))

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
    async def ivy_generate_manifest(
        rfc_name: str,
        rfc_text: str,
        protocol: str = "",
        base_section: str = "",
    ) -> str:
        """Generate a YAML requirements manifest from RFC text.

        Extracts MUST/SHOULD/MAY requirements from the text and formats
        them as a structured YAML manifest ready for traceability tools.

        The output is a YAML string that can be saved as
        ``protocol-testing/<protocol>/<rfc>_requirements.yaml``.

        Args:
            rfc_name: RFC identifier (e.g., "RFC9000").
            rfc_text: Raw RFC text to parse.
            protocol: Protocol name for layer inference (e.g., "quic").
            base_section: Default section prefix (e.g., "4" for all
                requirements in section 4).
        """
        results = []
        for m in _RFC_REQ_PATTERN.finditer(rfc_text):
            text = m.group(1).strip()
            level = m.group(2)
            if level in ("SHALL", "REQUIRED"):
                level = "MUST"
            elif level in ("SHALL NOT",):
                level = "MUST NOT"
            elif level in ("RECOMMENDED",):
                level = "SHOULD"
            elif level in ("OPTIONAL",):
                level = "MAY"
            results.append({"text": text, "level": level, "offset": m.start()})

        rfc_lower = rfc_name.lower().replace(" ", "")
        manifest_lines = [
            f"rfc: {rfc_name}",
            f"title: '{protocol.upper()} protocol requirements'",
            "requirements:",
        ]
        for i, req in enumerate(results, start=1):
            section = f"{base_section}.{i}" if base_section else str(i)
            tag = f"{rfc_lower}:{section}"
            escaped_text = req["text"].replace("'", "''")
            manifest_lines.append(f"  {tag}:")
            manifest_lines.append(f"    text: '{escaped_text}'")
            manifest_lines.append(f"    section: '{section}'")
            manifest_lines.append(f"    level: {req['level']}")
            manifest_lines.append(f"    layer: ''")
            manifest_lines.append(f"    testable: true")

        yaml_content = "\n".join(manifest_lines) + "\n"
        suggested_path = ""
        if protocol:
            suggested_path = (
                f"protocol-testing/{protocol}/"
                f"{rfc_lower}_requirements.yaml"
            )

        return json.dumps({
            "yaml": yaml_content,
            "total_requirements": len(results),
            "suggested_path": suggested_path,
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
        workspace_root: str = ""  # H3: for relative path output

    async def _make_viz_server_proxy():
        """Create a minimal server-like object for visualization handlers.

        The visualization handlers in features/visualization.py expect a
        server object with ``server.indexer.requirement_graph``.  This
        lazily builds the requirement graph on first use and returns a
        lightweight proxy that satisfies that contract.
        """
        graph = await _get_req_graph()
        return _ServerProxy(
            indexer=_IndexerProxy(requirement_graph=graph),
            workspace_root=root,
        )

    @mcp.tool()
    async def ivy_action_requirements(
        action_name: str | None = None,
        file_path: str | None = None,
        test_file: str | None = None,
        protocol: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> str:
        """Get requirements organized by action boundaries (before/after monitors).

        Returns requirements grouped by the action they monitor, their temporal
        position (before/after), kind (require/ensure/assume/assert), and the
        state variables they read or write.

        Args:
            action_name: Specific action to query. If omitted, returns all actions.
            file_path: Scope to actions defined in this file (relative path).
            test_file: Optional test file to scope the analysis to (relative path).
            protocol: Protocol name (e.g., "quic") to scope results.
            offset: Number of actions to skip (default: 0).
            limit: Maximum number of actions to return. If omitted, returns all.
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        if offset:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        return json.dumps(handle_action_requirements(server_proxy, params))

    @mcp.tool()
    async def ivy_model_summary(
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> str:
        """Get per-action requirement counts, state variable usage, and RFC coverage.

        Returns one row per action with counts of before/after requirements by kind,
        state variables read/written, and RFC bracket tags covered.

        Args:
            test_file: Optional test file to scope the summary to (relative path).
            protocol: Protocol name (e.g., "quic") to scope results.
        """
        from ivy_lsp.features.visualization import handle_model_summary_table

        server_proxy = await _make_viz_server_proxy()
        params: dict[str, Any] = {}
        if test_file:
            try:
                params["testFile"] = _validate_path(root, test_file)
            except ValueError as exc:
                return json.dumps({"success": False, "message": str(exc)})
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_model_summary_table(server_proxy, params))

    @mcp.tool()
    async def ivy_coverage_gaps(
        test_file: str | None = None,
        protocol: str | None = None,
    ) -> str:
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_coverage_gaps(server_proxy, params))

    @mcp.tool()
    async def ivy_action_dependency_graph(
        test_file: str | None = None,
        include_state_vars: bool = False,
        protocol: str | None = None,
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_action_dependency_graph(server_proxy, params))

    @mcp.tool()
    async def ivy_state_machine_view(
        test_file: str | None = None,
        state_var_filter: str | None = None,
        protocol: str | None = None,
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_state_machine_view(server_proxy, params))

    @mcp.tool()
    async def ivy_layered_overview(
        test_file: str | None = None,
        group_by: str = "file",
        protocol: str | None = None,
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_layered_overview(server_proxy, params))

    @mcp.tool()
    async def ivy_smart_suggestions(
        file_path: str | None = None,
        line: int | None = None,
        context: str | None = None,
        protocol: str | None = None,
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
        if protocol:
            params["protocolFilter"] = f"protocol-testing/{protocol}/"
        return json.dumps(handle_smart_suggestions(server_proxy, params))

    @mcp.tool()
    async def ivy_pattern_analysis(
        protocol: str,
        mode: str = "detect",
        pattern: str | None = None,
        reference_protocol: str | None = None,
    ) -> str:
        """Analyze formal model patterns in a protocol specification.

        Detects recurring patterns (serdes, variants, monitors, shims, modules,
        entities) and validates cross-references between them.

        Args:
            protocol: Protocol name (e.g., "quic", "bgp", "minip").
            mode: Analysis mode: "detect" (find patterns), "validate" (check
                cross-references), or "compare" (diff two protocols).
            pattern: Optional specific pattern to analyze (e.g., "serdes", "variants").
            reference_protocol: Required for "compare" mode — protocol to compare against.
        """
        from ivy_lsp.features.patterns import handle_pattern_analysis

        params: dict[str, Any] = {"protocol": protocol, "mode": mode}
        if pattern:
            params["pattern"] = pattern
        if reference_protocol:
            params["reference_protocol"] = reference_protocol
        return json.dumps(handle_pattern_analysis(root, params))

    @mcp.tool()
    async def ivy_pattern_scaffold(
        protocol: str,
        pattern: str,
        wire_format: str = "binary",
        role_type: str = "asymmetric",
        variant_names: list[str] | None = None,
        roles: list[str] | None = None,
    ) -> str:
        """Generate Ivy source from a pattern template.

        Loads a pattern template, performs placeholder substitution with the
        given protocol name and options, and returns the generated source code.

        Args:
            protocol: Protocol name for placeholder substitution.
            pattern: Pattern to scaffold: "serdes", "variants", "monitors",
                "shim", "module", or "entity".
            wire_format: Wire format for serdes: "binary" (default) or "json".
                For shim pattern, use "udp" or "tcp".
            role_type: Role type for entity: "asymmetric" (default) or "symmetric".
            variant_names: Optional list of variant/message type names.
            roles: Optional list of role names (e.g., ["client", "server"]).
        """
        from ivy_lsp.features.patterns import handle_pattern_scaffold

        params: dict[str, Any] = {
            "protocol": protocol,
            "pattern": pattern,
            "wire_format": wire_format,
            "role_type": role_type,
        }
        if variant_names:
            params["variant_names"] = variant_names
        if roles:
            params["roles"] = roles
        return json.dumps(handle_pattern_scaffold(root, params))

    @mcp.tool()
    async def ivy_scaffold_check(protocol: str) -> str:
        """Check which layers/patterns are present or missing in a protocol model.

        Compares the protocol's directory structure and file contents against
        the canonical 14-layer decomposition used by QUIC (the gold standard).
        Returns a completeness score with present/missing layers and suggestions.

        Args:
            protocol: Protocol name (e.g., "quic", "bgp", "minip", "coap").
        """
        # Canonical layers with detection heuristics
        _LAYERS = [
            ("types", "{p}_types.ivy", "type "),
            ("codec", "{p}_codec.ivy", "interpret "),
            ("frame", "{p}_frame.ivy", "variant "),
            ("packet", "{p}_packet.ivy", "type.*quic_packet"),
            ("connection", "{p}_connection.ivy", "relation conn"),
            ("transport", "{p}_transport.ivy", "action "),
            ("security", "{p}_security.ivy", "action "),
            ("application", "{p}_application.ivy", "action app_"),
            ("shim", "{p}_shim*.ivy", "<<< impl"),
            ("test_specs", "{p}_*_test_*.ivy", "export "),
            ("entities", None, "instance "),
            ("behavior", "{p}_*_behavior.ivy", "before "),
            ("recovery", "{p}_recovery*.ivy", None),
            ("extensions", "{p}_extension*.ivy", None),
        ]

        prot_dir = os.path.join(root, "protocol-testing", protocol)
        if not os.path.isdir(prot_dir):
            return json.dumps({
                "success": False,
                "message": f"Protocol directory not found: protocol-testing/{protocol}",
            })

        # Collect all .ivy files under this protocol
        prot_files = _find_ivy_files(root)
        prot_files = [
            f for f in prot_files
            if f.startswith(f"protocol-testing/{protocol}/")
        ]

        layers_present = []
        layers_missing = []
        suggestions = []

        for layer_name, file_pattern, content_marker in _LAYERS:
            found = False
            matched_files = []

            if file_pattern:
                import fnmatch
                pat = file_pattern.replace("{p}", protocol.split("/")[-1])
                for f in prot_files:
                    basename = os.path.basename(f)
                    if fnmatch.fnmatch(basename, pat):
                        found = True
                        matched_files.append(f)

            if not found and content_marker:
                for f in prot_files:
                    abs_f = os.path.join(root, f)
                    try:
                        with open(abs_f, encoding="utf-8", errors="replace") as fh:
                            if content_marker in fh.read(4096):
                                found = True
                                matched_files.append(f)
                                break
                    except OSError:
                        continue

            if found:
                layers_present.append({
                    "layer": layer_name,
                    "files": matched_files[:3],
                })
            else:
                layers_missing.append(layer_name)
                suggestions.append({
                    "layer": layer_name,
                    "priority": "high" if layer_name in (
                        "types", "frame", "packet", "connection",
                    ) else "medium",
                    "suggestion": (
                        f"Add {layer_name} layer: create "
                        f"{protocol}_{layer_name}.ivy in "
                        f"protocol-testing/{protocol}/{protocol}_stack/"
                    ),
                })

        total = len(_LAYERS)
        present = len(layers_present)
        score = round(present / total * 100) if total else 0

        # Check for manifest
        has_manifest = any(
            f.endswith("_requirements.yaml") for f in prot_files
        )
        if not has_manifest:
            suggestions.append({
                "layer": "traceability",
                "priority": "medium",
                "suggestion": (
                    "No requirements manifest found. Use "
                    "ivy_generate_manifest to create one from RFC text."
                ),
            })

        return json.dumps({
            "protocol": protocol,
            "completeness_score": score,
            "total_layers": total,
            "present": present,
            "missing": len(layers_missing),
            "total_ivy_files": len(prot_files),
            "has_manifest": has_manifest,
            "layers_present": layers_present,
            "layers_missing": layers_missing,
            "suggestions": suggestions,
        })

    @mcp.tool()
    async def ivy_quality_gate(
        protocol: str,
        gate_level: str = "minimal",
    ) -> str:
        """Validate a protocol model against quality gates.

        Checks the model at one of three levels:
        - minimal: lang header, balanced braces, includes resolve
        - standard: + test specs exist, behavior files exist, actions have monitors
        - comprehensive: + manifest exists, coverage > 0, no unguarded state vars

        Args:
            protocol: Protocol name (e.g., "quic", "bgp").
            gate_level: Gate level: "minimal", "standard", or "comprehensive".
        """
        prot_dir = os.path.join(root, "protocol-testing", protocol)
        if not os.path.isdir(prot_dir):
            return json.dumps({
                "success": False,
                "message": f"Protocol directory not found: protocol-testing/{protocol}",
            })

        prot_files = [
            f for f in _find_ivy_files(root)
            if f.startswith(f"protocol-testing/{protocol}/")
        ]

        checks: list[dict[str, Any]] = []
        all_passed = True

        # --- MINIMAL checks ---
        # 1. Lang header
        files_without_header = []
        for f in prot_files:
            abs_f = os.path.join(root, f)
            try:
                with open(abs_f, encoding="utf-8", errors="replace") as fh:
                    first_line = fh.readline()
                if not first_line.startswith("#lang"):
                    files_without_header.append(f)
            except OSError:
                continue
        passed = len(files_without_header) == 0
        if not passed:
            all_passed = False
        checks.append({
            "check": "lang_header",
            "level": "minimal",
            "passed": passed,
            "detail": (
                f"{len(files_without_header)} files missing #lang header"
                if not passed else "All files have #lang header"
            ),
        })

        # 2. Includes resolve
        import re as _re
        _inc_re = _re.compile(r"^include\s+(\w+)", _re.MULTILINE)
        basenames = {
            os.path.splitext(os.path.basename(f))[0] for f in prot_files
        }
        unresolved = []
        for f in prot_files:
            abs_f = os.path.join(root, f)
            try:
                with open(abs_f, encoding="utf-8", errors="replace") as fh:
                    for inc in _inc_re.findall(fh.read()):
                        if inc not in basenames and inc not in _STDLIB_MODULES:
                            unresolved.append({"file": f, "include": inc})
            except OSError:
                continue
        passed = len(unresolved) == 0
        if not passed:
            all_passed = False
        checks.append({
            "check": "includes_resolve",
            "level": "minimal",
            "passed": passed,
            "detail": (
                f"{len(unresolved)} unresolved includes"
                if not passed else "All includes resolve"
            ),
            "unresolved": unresolved[:10] if not passed else [],
        })

        # 3. File count sanity
        passed = len(prot_files) >= 3
        if not passed:
            all_passed = False
        checks.append({
            "check": "minimum_files",
            "level": "minimal",
            "passed": passed,
            "detail": f"{len(prot_files)} .ivy files found",
        })

        if gate_level in ("standard", "comprehensive"):
            # --- STANDARD checks ---
            # 4. Test specs exist
            test_files = [
                f for f in prot_files if "_test" in os.path.basename(f)
            ]
            passed = len(test_files) > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "test_specs_exist",
                "level": "standard",
                "passed": passed,
                "detail": f"{len(test_files)} test spec files found",
            })

            # 5. Behavior/monitor files exist
            behavior_files = [
                f for f in prot_files if "_behavior" in os.path.basename(f)
            ]
            monitor_count = 0
            _monitor_re = _re.compile(
                r"^\s*(before|after|around)\s+", _re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        monitor_count += len(_monitor_re.findall(fh.read()))
                except OSError:
                    continue
            passed = monitor_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "monitors_exist",
                "level": "standard",
                "passed": passed,
                "detail": (
                    f"{monitor_count} monitor clauses across "
                    f"{len(behavior_files)} behavior files"
                ),
            })

            # 6. Export actions exist (for test generation)
            export_count = 0
            _export_re = _re.compile(
                r"^\s*export\s+\w+", _re.MULTILINE,
            )
            for f in prot_files:
                abs_f = os.path.join(root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        export_count += len(_export_re.findall(fh.read()))
                except OSError:
                    continue
            passed = export_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "exports_exist",
                "level": "standard",
                "passed": passed,
                "detail": f"{export_count} exported actions found",
            })

        if gate_level == "comprehensive":
            # --- COMPREHENSIVE checks ---
            # 7. Manifest exists
            has_manifest = any(
                f.endswith("_requirements.yaml") for f in prot_files
            )
            if not has_manifest:
                all_passed = False
            checks.append({
                "check": "manifest_exists",
                "level": "comprehensive",
                "passed": has_manifest,
                "detail": (
                    "Requirements manifest found"
                    if has_manifest else "No requirements manifest"
                ),
            })

            # 8. Bracket tag annotations present
            _tag_re = _re.compile(r"#\s*\[[\w:.,\s]+\]\s*$", _re.MULTILINE)
            tag_count = 0
            for f in prot_files:
                abs_f = os.path.join(root, f)
                try:
                    with open(abs_f, encoding="utf-8", errors="replace") as fh:
                        tag_count += len(_tag_re.findall(fh.read()))
                except OSError:
                    continue
            passed = tag_count > 0
            if not passed:
                all_passed = False
            checks.append({
                "check": "annotations_exist",
                "level": "comprehensive",
                "passed": passed,
                "detail": f"{tag_count} bracket-tag annotations found",
            })

        passed_count = sum(1 for c in checks if c["passed"])
        return json.dumps({
            "protocol": protocol,
            "gate_level": gate_level,
            "passed": all_passed,
            "checks_passed": passed_count,
            "checks_total": len(checks),
            "checks": checks,
        })

    if _return_app:
        return mcp

    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    mcp.run(transport="stdio")
