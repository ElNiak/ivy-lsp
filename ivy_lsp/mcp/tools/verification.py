"""Verification tools: ivy_verify, ivy_compile, ivy_model_info, ivy_diagnostics."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from typing import Any

from ivy_lsp.core.verification import run_ivy_check as shared_ivy_check
from ivy_lsp.core.verification import run_ivy_compile as shared_ivy_compile
from ivy_lsp.core.verification import run_ivy_show as shared_ivy_show
from ivy_lsp.infra.observability import ToolTraceContext, trace_tool
from ivy_lsp.infra.utils.ivy_output import extract_error_summary, parse_ivy_output
from ivy_lsp.infra.utils.validation import validate_ivy_param as _validate_ivy_param
from ivy_lsp.mcp.tools import error_response, inject_scope_metadata, safe_tool

logger = logging.getLogger(__name__)

# Maximum number of entries in the verification cache (LRU eviction)
_CACHE_MAX_SIZE = 100

# These patterns intentionally use regex — they perform semantic diagnostic
# checks and tool output parsing, not symbol extraction.  See
# ivy_lsp.core.parsing.tiered_extractor for the symbol extraction cascade.
_ASSERTION_RE = re.compile(
    r"^\s*(require|ensure|assume|assert)\s+.+;\s*$", re.MULTILINE
)
_BRACKET_TAG_RE = re.compile(r"#\s*\[")

_ISOLATE_STATUS_RE = re.compile(
    r"^\s*isolate\s+([\w.]+)\s*:\s*(PASS|FAIL|OK)\s*$", re.MULTILINE
)


def register_verification_tools(mcp: Any, ctx: Any) -> None:
    """Register verification-related MCP tools."""
    from dataclasses import dataclass as _dataclass

    @_dataclass
    class _CacheEntry:
        result: dict
        file_mtime: float
        include_mtimes: dict[str, float]  # transitive includes -> mtime

    # Per-isolate verification cache: (abs_path, isolate|None) -> _CacheEntry
    # Moved into closure scope so each MCP server instance has its own cache.
    _verify_cache: dict[tuple[str, str | None], _CacheEntry] = {}
    _verify_cache_lock = asyncio.Lock()
    _verify_in_flight: set[tuple[str, str | None]] = set()

    def _get_file_mtime(abs_path: str) -> float:
        """Get file mtime, returning 0.0 if file doesn't exist."""
        try:
            return os.path.getmtime(abs_path)
        except OSError:
            return 0.0

    def _get_include_mtimes(abs_path: str) -> dict[str, float]:
        """Get mtimes for the file's transitive include closure."""
        mtimes: dict[str, float] = {}
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                source = f.read()
            # Use a simple regex to find includes (lightweight, no full parse)
            for m in re.finditer(r"^\s*include\s+(\w+)", source, re.MULTILINE):
                inc_name = m.group(1)
                # Try to resolve via basename cache
                cache = ctx.get_basename_cache()
                candidates = cache.get(inc_name, [])
                if candidates:
                    inc_path = os.path.join(ctx.root, candidates[0])
                    mtimes[inc_path] = _get_file_mtime(inc_path)
        except OSError:
            pass
        return mtimes

    def _cache_is_fresh(entry: _CacheEntry, abs_path: str) -> bool:
        """Check if cached result is still fresh (no files changed)."""
        # Check main file
        if _get_file_mtime(abs_path) != entry.file_mtime:
            return False
        # Check includes
        for inc_path, cached_mtime in entry.include_mtimes.items():
            if _get_file_mtime(inc_path) != cached_mtime:
                return False
        return True

    def _evict_oldest_if_needed() -> None:
        """Evict oldest cache entries when cache exceeds _CACHE_MAX_SIZE."""
        while len(_verify_cache) > _CACHE_MAX_SIZE:
            oldest_key = next(iter(_verify_cache))
            _verify_cache.pop(oldest_key)

    def _cache_per_isolate_results(
        abs_path: str,
        raw_output: str,
        full_result: dict[str, Any],
    ) -> None:
        """Extract per-isolate status from full verification output and cache each."""
        for m in _ISOLATE_STATUS_RE.finditer(raw_output):
            iso_name = m.group(1)
            status = m.group(2)
            iso_key = (abs_path, iso_name)
            if iso_key not in _verify_cache:
                iso_success = status in ("PASS", "OK")
                iso_diags = [
                    d
                    for d in full_result.get("diagnostics", [])
                    if iso_name in d.get("message", "") or iso_name in d.get("file", "")
                ]
                _verify_cache[iso_key] = _CacheEntry(
                    result={
                        "success": iso_success,
                        "diagnostics": iso_diags,
                        "diagnostic_count": len(iso_diags),
                        "error_summary": (
                            full_result.get("error_summary", "")
                            if not iso_success
                            else ""
                        ),
                        "duration_seconds": full_result.get("duration_seconds", 0),
                        "cached": False,
                        "isolate": iso_name,
                    },
                    file_mtime=_get_file_mtime(abs_path),
                    include_mtimes=_get_include_mtimes(abs_path),
                )
                _evict_oldest_if_needed()

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
        use_cache: bool = False,
        compact: bool = True,
        scope: str = "",
    ) -> dict:
        """Run ivy_check on an Ivy file to verify formal properties.

        Returns structured diagnostics with file, line, severity, and message.
        Requires the ``ivy_check`` CLI tool (available inside Docker or with
        native Ivy installation).

        Args:
            relative_path: Relative path to the .ivy file to check.
            isolate: Optional isolate name to check in isolation.
            use_cache: When True, return cached result if available.
            compact: When True (default), strip raw_output and full
                counterexample from the result to reduce context window usage.
                Only counterexample_trace (formatted summary) is kept.
            scope: Optional test scope name.  When set and the workspace
                context has a matching scope, the scope name is included in
                the result summary.  Empty string (default) = no scoping.
        """
        if not shutil.which("ivy_check"):
            return error_response(
                "ivy_check CLI not found on PATH. "
                "This tool requires the Ivy compiler, typically available "
                "inside Docker containers built by PANTHER."
            )
        logger.debug(
            "[ivy_verify] workspace=%s, args=%r",
            ctx.root,
            {
                "relative_path": relative_path,
                "isolate": isolate,
                "use_cache": use_cache,
                "scope": scope,
            },
        )

        # Resolve scope (if provided) for result annotation
        _resolved_scope = None
        if scope and getattr(ctx, "workspace_context", None) is not None:
            _resolved_scope = ctx.workspace_context.get_test_scope(scope)
            if _resolved_scope is None:
                logger.warning(
                    "[ivy_verify] Unknown scope '%s'; proceeding without scoping",
                    scope,
                )

        with trace_tool(
            "ivy_verify",
            {
                "relative_path": relative_path,
                "isolate": isolate,
                "use_cache": use_cache,
                "scope": scope,
            },
        ) as _tt:
            try:
                abs_path = ctx.validate_path(relative_path)
            except ValueError as exc:
                _tt[0] = error_response(str(exc))
                return _tt[0]
            if not os.path.isfile(abs_path):
                _tt[0] = error_response(f"File not found: {relative_path}")
                return _tt[0]

            if isolate:
                try:
                    _validate_ivy_param(isolate)
                except ValueError as exc:
                    _tt[0] = error_response(str(exc))
                    return _tt[0]

            cache_key = (abs_path, isolate)

            # Phase 1: Acquire lock, check cache / in-flight
            async with _verify_cache_lock:
                if use_cache and cache_key in _verify_cache:
                    entry = _verify_cache[cache_key]
                    if _cache_is_fresh(entry, abs_path):
                        cached_result = dict(entry.result)
                        cached_result["cached"] = True
                        _tt[0] = cached_result
                        return _tt[0]
                    else:
                        # Stale — remove from cache
                        del _verify_cache[cache_key]

                if cache_key in _verify_in_flight:
                    need_wait = True
                else:
                    _verify_in_flight.add(cache_key)
                    need_wait = False

            # If another coroutine owns this key, poll for its result
            if need_wait:
                for _ in range(600):  # up to ~60s
                    await asyncio.sleep(0.1)
                    async with _verify_cache_lock:
                        if cache_key in _verify_cache:
                            entry = _verify_cache[cache_key]
                            if _cache_is_fresh(entry, abs_path):
                                cached_result = dict(entry.result)
                                cached_result["cached"] = True
                                _tt[0] = cached_result
                                return _tt[0]
                        if cache_key not in _verify_in_flight:
                            _verify_in_flight.add(cache_key)
                            break
                else:
                    async with _verify_cache_lock:
                        _verify_in_flight.add(cache_key)

            # Phase 2: Run subprocess WITHOUT holding lock
            try:
                result = await shared_ivy_check(
                    filepath=abs_path,
                    workspace_root=ctx.root,
                    isolate=isolate,
                    staging_dir=ctx.staging_dir,
                    resolver=ctx.include_resolver,
                )

                if not result.get("success", True):
                    from ivy_lsp.infra.utils.counterexample_parser import (
                        parse_counterexample,
                    )

                    raw = result.get("raw_output", "")
                    cex = parse_counterexample(raw)
                    if cex is not None:
                        result["counterexample"] = cex
                        from ivy_lsp.infra.utils.counterexample_formatter import (
                            format_counterexample,
                        )

                        result["counterexample_trace"] = format_counterexample(cex)

                result["cached"] = False

                # Phase 3: Write to cache under lock
                async with _verify_cache_lock:
                    _verify_cache[cache_key] = _CacheEntry(
                        result=dict(result),
                        file_mtime=_get_file_mtime(abs_path),
                        include_mtimes=_get_include_mtimes(abs_path),
                    )
                    _evict_oldest_if_needed()

                    if isolate is None:
                        raw_output = result.get("raw_output", "")
                        _cache_per_isolate_results(abs_path, raw_output, result)

                    _verify_in_flight.discard(cache_key)
            except Exception:
                async with _verify_cache_lock:
                    _verify_in_flight.discard(cache_key)
                raise

            # Strip verbose fields in compact mode
            if compact:
                result.pop("raw_output", None)
                result.pop("counterexample", None)

            # Trim raw output regardless of compact mode
            from ivy_lsp.infra.config import get_config

            max_raw = get_config().max_raw_output_length
            if max_raw > 0 and "raw_output" in result:
                raw = result["raw_output"]
                if len(raw) > max_raw:
                    result["raw_output"] = (
                        raw[:max_raw] + f"\n... [truncated at {max_raw} chars,"
                        " full output in /tmp/ivy-lsp-latest.log]"
                    )

            inject_scope_metadata(result, scope, _resolved_scope)

            _tt[0] = result
            return _tt[0]

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_compile(
        relative_path: str,
        target: str = "test",
        isolate: str | None = None,
        scope: str = "",
    ) -> dict:
        """Compile an Ivy file to a test executable using ivyc.

        When a Docker image is configured (--docker-image), compilation runs
        inside a Docker container with all required C++ dependencies.
        Otherwise falls back to native subprocess execution.

        Requires the ``ivyc`` CLI tool (available inside Docker or with
        native Ivy installation).

        Args:
            relative_path: Relative path to the .ivy file to compile.
            target: Compilation target (default: "test").
            isolate: Optional isolate name to compile in isolation.
            scope: Optional test scope name.  When set and the workspace
                context has a matching scope, the scope name is included in
                the result summary.  Empty string (default) = no scoping.
        """
        if not shutil.which("ivyc") and not ctx.docker_image:
            return error_response(
                "ivyc CLI not found on PATH and no Docker image configured. "
                "This tool requires the Ivy compiler, typically available "
                "inside Docker containers built by PANTHER."
            )
        logger.debug(
            "[ivy_compile] workspace=%s, args=%r",
            ctx.root,
            {
                "relative_path": relative_path,
                "target": target,
                "isolate": isolate,
                "scope": scope,
            },
        )

        # Resolve scope (if provided) for result annotation
        _resolved_scope = None
        if scope and getattr(ctx, "workspace_context", None) is not None:
            _resolved_scope = ctx.workspace_context.get_test_scope(scope)
            if _resolved_scope is None:
                logger.warning(
                    "[ivy_compile] Unknown scope '%s'; proceeding without scoping",
                    scope,
                )
        with trace_tool(
            "ivy_compile",
            {"relative_path": relative_path, "target": target, "isolate": isolate},
        ) as _tt:
            try:
                abs_path = ctx.validate_path(relative_path)
            except ValueError as exc:
                _tt[0] = error_response(str(exc))
                return _tt[0]
            if not os.path.isfile(abs_path):
                _tt[0] = error_response(f"File not found: {relative_path}")
                return _tt[0]

            try:
                _validate_ivy_param(target)
                if isolate:
                    _validate_ivy_param(isolate)
            except ValueError as exc:
                _tt[0] = error_response(str(exc))
                return _tt[0]

            t0 = time.monotonic()
            _docker_fallback_reason: str | None = None

            if ctx.executor is not None and ctx.base_path is not None:
                try:
                    from pathlib import Path as P

                    from panther_ivy.api.compiler import generate_compile_commands

                    compile_result = generate_compile_commands(
                        ivy_file=P(abs_path),
                        base_path=ctx.base_path,
                    )

                    start = time.monotonic()
                    setup_result = await asyncio.to_thread(
                        ctx.executor.execute,
                        compile_result.setup_commands,
                        workspace_root=ctx.root,
                        timeout=30,
                    )
                    if (
                        hasattr(setup_result, "exit_code")
                        and setup_result.exit_code != 0
                    ):
                        setup_fail = {
                            "success": False,
                            "message": f"Docker setup failed (exit {setup_result.exit_code})",
                            "raw_output": getattr(setup_result, "stderr", ""),
                            "duration_seconds": round(time.monotonic() - t0, 2),
                        }
                        _tt[0] = setup_fail
                        return _tt[0]
                    exec_result = await asyncio.to_thread(
                        ctx.executor.execute,
                        compile_result.compile_commands,
                        workspace_root=ctx.root,
                        timeout=300,
                    )
                    duration = time.monotonic() - start

                    raw_output = (
                        exec_result.stderr + "\n" + exec_result.stdout
                    ).strip()
                    diagnostics = parse_ivy_output(raw_output)
                    compile_result_dict = {
                        "success": exec_result.exit_code == 0
                        and not any(d["severity"] == "error" for d in diagnostics),
                        "diagnostics": diagnostics,
                        "diagnostic_count": len(diagnostics),
                        "error_summary": extract_error_summary(raw_output, diagnostics),
                        "raw_output": raw_output,
                        "target": exec_result.target,
                        "duration_seconds": round(duration, 2),
                    }
                    inject_scope_metadata(compile_result_dict, scope, _resolved_scope)
                    _tt[0] = compile_result_dict
                    return _tt[0]
                except ImportError:
                    logger.debug(
                        "panther_ivy.api not available; falling back to direct subprocess"
                    )
                except (ConnectionError, OSError, RuntimeError) as exc:
                    logger.error(
                        "Docker compile failed unexpectedly: %s",
                        exc,
                        exc_info=True,
                    )
                    _docker_fallback_reason = str(exc)

            result = await shared_ivy_compile(
                filepath=abs_path,
                workspace_root=ctx.root,
                target=target,
                isolate=isolate,
                staging_dir=ctx.staging_dir,
                resolver=ctx.include_resolver,
            )

            if _docker_fallback_reason:
                result["fallback"] = "subprocess"
                result["fallback_reason"] = _docker_fallback_reason

            inject_scope_metadata(result, scope, _resolved_scope)

            _tt[0] = result
            return _tt[0]

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_model_info(
        relative_path: str,
        isolate: str | None = None,
    ) -> dict:
        """Display the structure of an Ivy model using ivy_show.

        Requires the ``ivy_show`` CLI tool (available inside Docker containers
        built by the PANTHER framework, or when Ivy is installed natively).

        Args:
            relative_path: Relative path to the .ivy file to inspect.
            isolate: Optional isolate name for a specific isolate.
        """
        if not shutil.which("ivy_show"):
            return error_response(
                "ivy_show CLI not found on PATH. "
                "This tool requires the Ivy compiler, which is typically "
                "available inside Docker containers built by PANTHER. "
                "Run 'panther run' with a config to build the Docker environment first."
            )
        logger.debug(
            "[ivy_model_info] workspace=%s, args=%r",
            ctx.root,
            {"relative_path": relative_path, "isolate": isolate},
        )
        with trace_tool(
            "ivy_model_info", {"relative_path": relative_path, "isolate": isolate}
        ) as _tt:
            try:
                abs_path = ctx.validate_path(relative_path)
            except ValueError as exc:
                _tt[0] = error_response(str(exc))
                return _tt[0]
            if not os.path.isfile(abs_path):
                _tt[0] = error_response(f"File not found: {relative_path}")
                return _tt[0]

            if isolate:
                try:
                    _validate_ivy_param(isolate)
                except ValueError as exc:
                    _tt[0] = error_response(str(exc))
                    return _tt[0]

            result = await shared_ivy_show(
                filepath=abs_path,
                workspace_root=ctx.root,
                isolate=isolate,
                staging_dir=ctx.staging_dir,
                resolver=ctx.include_resolver,
            )

            # If the error mentions isolates, detect available isolates
            if not result.get("success", True):
                err_msg = result.get("error_summary", "") or result.get(
                    "raw_output", ""
                )
                if "isolate" in err_msg.lower() or "no isolate" in err_msg.lower():
                    # Scan file for isolate declarations
                    try:
                        with open(abs_path, encoding="utf-8", errors="replace") as f:
                            source = f.read()
                        isolates = re.findall(
                            r"^\s*isolate\s+([\w.]+)\s*", source, re.MULTILINE
                        )
                        if isolates:
                            result["available_isolates"] = isolates
                            result["hint"] = (
                                f"This file has {len(isolates)} isolate(s). "
                                f"Specify one with isolate='{isolates[0]}'"
                            )
                    except OSError:
                        pass

            _tt[0] = result
            return _tt[0]

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_diagnostics(
        relative_path: str,
        mode: str = "full",
        layers: list[str] | None = None,
        min_severity: str | None = None,
        scope: str = "",
    ) -> dict:
        """Diagnostic analysis of an Ivy file or workspace.

        Supports three modes:
        - "structural": Fast structural lint only (milliseconds, no subprocess).
          Checks missing #lang header, unmatched braces, unresolved includes.
          Replaces the former ivy_lint tool.
        - "full": All 5 diagnostic layers (structural, lexer, semantic,
          coverage, pattern). More thorough but may take longer on first
          call (lazy model/graph building). Default.
        - "collisions": Workspace-level include-name collision report.
          Classifies basename collisions by layer relationship: intra-layer
          (error), cross-layer-in-scope (warning), cross-boundary (info).
          Does not require a file path (relative_path is ignored).

        Args:
            relative_path: Relative path to the .ivy file to diagnose.
                Ignored when mode="collisions".
            mode: Diagnostic mode — "structural" for fast lint (replaces
                ivy_lint), "full" for all layers (default), "collisions"
                for workspace-level collision analysis.
            layers: Optional list of layers to run (full mode only).
                Valid values: structural, lexer, semantic, coverage, pattern.
                Defaults to all layers.
            min_severity: Minimum severity to include: error, warning, info, hint.
            scope: Optional test scope name.  When set, diagnostics are
                filtered to files within the scope's include closure.
                Empty string (default) = no scoping.
        """
        logger.debug(
            "[ivy_diagnostics] workspace=%s, args=%r",
            ctx.root,
            {
                "relative_path": relative_path,
                "mode": mode,
                "layers": layers,
                "min_severity": min_severity,
                "scope": scope,
            },
        )

        # Resolve scope for file filtering
        _scope_files: frozenset[str] | None = None
        _resolved_scope = None
        if scope and getattr(ctx, "workspace_context", None) is not None:
            _resolved_scope = ctx.workspace_context.get_test_scope(scope)
            if _resolved_scope is not None:
                _scope_files = _resolved_scope.include_closure
            else:
                logger.warning(
                    "[ivy_diagnostics] Unknown scope '%s'; proceeding without scoping",
                    scope,
                )

        _tc = ToolTraceContext(
            "ivy_diagnostics",
            {
                "relative_path": relative_path,
                "mode": mode,
                "layers": layers,
                "min_severity": min_severity,
                "scope": scope,
            },
        )
        if mode not in ("structural", "full", "collisions"):
            return _tc.finish(
                error_response(
                    f"Unknown mode '{mode}'. Valid modes: ['structural', 'full', 'collisions']"
                )
            )

        # collisions mode: workspace-level, does not need a file path
        if mode == "collisions":
            from ivy_lsp.mcp.tools.analysis import _handle_collisions_mode

            collision_result = await _handle_collisions_mode(ctx)
            return _tc.finish(collision_result)

        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return _tc.finish(error_response(str(exc)))
        if not os.path.isfile(abs_path):
            return _tc.finish(error_response(f"File not found: {relative_path}"))

        # Skip file if it falls outside the requested scope
        if _scope_files is not None and abs_path not in _scope_files:
            return _tc.finish(
                {
                    "success": True,
                    "file": relative_path,
                    "mode": mode,
                    "diagnostics": [],
                    "diagnostic_count": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "scope": scope,
                    "scope_filtered": True,
                    "note": f"File not in scope '{scope}' include closure; skipped.",
                }
            )

        with open(abs_path, encoding="utf-8", errors="replace") as f:
            source = f.read()

        # Fast path: structural-only mode (replaces former ivy_lint tool)
        if mode == "structural":
            resolve_cb = ctx.make_resolve_callback()
            diagnostics = ctx.check_structural_issues(source, abs_path, resolve_cb)
            _struct_result: dict[str, Any] = {
                "success": True,
                "file": relative_path,
                "mode": "structural",
                "diagnostics": diagnostics,
                "diagnostic_count": len(diagnostics),
                "error_count": sum(1 for d in diagnostics if d["severity"] == "error"),
                "warning_count": sum(
                    1 for d in diagnostics if d["severity"] == "warning"
                ),
            }
            inject_scope_metadata(_struct_result, scope, _resolved_scope)
            return _tc.finish(_struct_result)

        # Full mode: all 5 diagnostic layers
        all_diags: list[dict[str, Any]] = []
        layer_errors: list[dict[str, str]] = []

        # 1. Structural checks
        if layers is None or "structural" in layers:
            resolve_cb = ctx.make_resolve_callback()
            all_diags.extend(ctx.check_structural_issues(source, abs_path, resolve_cb))

        # 2. Lexer errors via fallback scanner (no Z3 needed)
        if layers is None or "lexer" in layers:
            try:
                from ivy_lsp.core.parsing.fallback_scanner import fallback_scan

                _symbols, error_info = await asyncio.to_thread(
                    fallback_scan,
                    source,
                    abs_path,
                )
                if error_info is not None:
                    all_diags.append(
                        {
                            "line": error_info.get("line", 1),
                            "severity": "error",
                            "message": f"Lexer error: {error_info.get('message', 'unknown')}",
                            "source": "ivy-lsp-lexer",
                        }
                    )
            except Exception as exc:
                logger.warning("Fallback scan failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "lexer", "error": str(exc)})

        # 3. Semantic diagnostics (orphaned RFC tags, untagged assertions)
        if layers is None or "semantic" in layers:
            try:
                # Check model status — avoid blocking on first-time build
                _model_status = ctx.get_model_status()
                if _model_status.get("state") == "ready":
                    model = await ctx.get_model()  # instant — already built
                elif hasattr(ctx, "get_model_or_none") and callable(
                    ctx.get_model_or_none
                ):
                    _result = ctx.get_model_or_none(timeout=5.0)
                    if asyncio.iscoroutine(_result):
                        model = await _result
                    else:
                        model = _result
                else:
                    model = None
                if model is None and _model_status.get("state") != "ready":
                    layer_errors.append(
                        {
                            "layer": "semantic",
                            "error": (
                                f"Model {_model_status.get('state', 'unavailable')} "
                                "(building in background; retry in 30s for full results)"
                            ),
                        }
                    )
                if model is not None:
                    from ivy_lsp.core.semantic.nodes import (
                        RfcAnnotation,
                        RfcRequirement,
                    )
                    from ivy_lsp.core.semantic.rfc_annotations import is_tag_covered

                    rfc_reqs = model.get_nodes_by_type(RfcRequirement)
                    annotations = [
                        n
                        for n in model.get_nodes_by_type(RfcAnnotation)
                        if n.file == abs_path
                    ]
                    if rfc_reqs:
                        req_ids = {r.id for r in rfc_reqs}
                        for ann in annotations:
                            for tag in ann.tags:
                                if not is_tag_covered(tag, req_ids):
                                    all_diags.append(
                                        {
                                            "line": ann.line + 1,
                                            "severity": "warning",
                                            "message": (
                                                f"Orphaned RFC tag: [{tag}] does not "
                                                "match any loaded requirement manifest"
                                            ),
                                            "source": "ivy-lsp-semantic",
                                        }
                                    )

                    # Missing tags on assertions
                    lines = source.split("\n")
                    for m in _ASSERTION_RE.finditer(source):
                        line_no = source[: m.start()].count("\n")
                        line_text = lines[line_no] if line_no < len(lines) else ""
                        if not _BRACKET_TAG_RE.search(line_text):
                            all_diags.append(
                                {
                                    "line": line_no + 1,
                                    "severity": "hint",
                                    "message": "Assertion without RFC bracket tag annotation",
                                    "source": "ivy-lsp-semantic",
                                }
                            )
            except Exception as exc:
                logger.warning(
                    "Semantic diagnostics failed for %s: %s", relative_path, exc
                )
                layer_errors.append({"layer": "semantic", "error": str(exc)})

        # 4. Coverage hints
        if layers is None or "coverage" in layers:
            try:
                graph = await ctx.get_req_graph()
                if graph is not None:
                    from ivy_lsp.core.coverage_hints import compute_coverage_hints

                    for hint in compute_coverage_hints(graph, abs_path):
                        all_diags.append(
                            {
                                "line": hint.get("line", 0),
                                "severity": hint.get("severity", "hint"),
                                "message": hint["message"],
                                "source": "ivy-lsp-coverage",
                                "code": hint.get("code"),
                            }
                        )
            except Exception as exc:
                logger.warning("Coverage hints failed for %s: %s", relative_path, exc)
                layer_errors.append({"layer": "coverage", "error": str(exc)})

        # 5. Pattern diagnostics (regex-based)
        if layers is None or "pattern" in layers:
            try:
                basename = os.path.basename(abs_path)

                # Missing _finalize in test files
                if "test" in basename.lower() and "_finalize" not in source:
                    has_export = bool(
                        re.search(r"^\s*export\s+action", source, re.MULTILINE)
                    )
                    if has_export:
                        all_diags.append(
                            {
                                "line": 1,
                                "severity": "warning",
                                "message": (
                                    "Test file has exports but no _finalize action. "
                                    "Consider adding 'export action _finalize' for "
                                    "end-of-test assertions."
                                ),
                                "source": "ivy-pattern",
                            }
                        )

                # Exported actions without monitors
                exports = set(
                    re.findall(
                        r"^\s*export\s+action\s+([\w.]+)",
                        source,
                        re.MULTILINE,
                    )
                )
                monitored = set(
                    re.findall(
                        r"^\s*(?:before|after|around)\s+([\w.]+)",
                        source,
                        re.MULTILINE,
                    )
                )
                for exp_action in exports:
                    if exp_action not in monitored and exp_action != "_finalize":
                        action_defined = bool(
                            re.search(
                                rf"^\s*action\s+{re.escape(exp_action)}\s*",
                                source,
                                re.MULTILINE,
                            )
                        )
                        if action_defined:
                            match = re.search(
                                rf"^\s*export\s+action\s+{re.escape(exp_action)}",
                                source,
                                re.MULTILINE,
                            )
                            line_num = (
                                source[: match.start()].count("\n") + 1 if match else 1
                            )
                            all_diags.append(
                                {
                                    "line": line_num,
                                    "severity": "hint",
                                    "message": (
                                        f"Exported action '{exp_action}' has no "
                                        "before/after monitor in this file."
                                    ),
                                    "source": "ivy-pattern",
                                }
                            )
            except Exception as exc:
                logger.warning(
                    "Pattern diagnostics failed for %s: %s", relative_path, exc
                )
                layer_errors.append({"layer": "pattern", "error": str(exc)})

        # P1: Ensure each diagnostic has the file field for multi-file processing
        for d in all_diags:
            if "file" not in d:
                d["file"] = relative_path

        # Apply severity filter
        if min_severity:
            _sev_order = {"error": 4, "warning": 3, "info": 2, "hint": 1}
            min_rank = _sev_order.get(min_severity, 0)
            all_diags = [
                d
                for d in all_diags
                if _sev_order.get(d.get("severity", "hint"), 0) >= min_rank
            ]

        # Build source-breakdown summary
        by_source: dict[str, int] = {}
        for d in all_diags:
            src = d.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        _diag_result: dict[str, Any] = {
            "success": True,
            "file": relative_path,
            "diagnostics": all_diags,
            "diagnostic_count": len(all_diags),
            "by_source": by_source,
            "error_count": sum(1 for d in all_diags if d.get("severity") == "error"),
            "warning_count": sum(
                1 for d in all_diags if d.get("severity") == "warning"
            ),
            "hint_count": sum(1 for d in all_diags if d.get("severity") == "hint"),
            "info_count": sum(1 for d in all_diags if d.get("severity") == "info"),
            "layer_errors": layer_errors,
            "partial": bool(layer_errors),
        }
        inject_scope_metadata(_diag_result, scope, _resolved_scope)

        return _tc.finish(_diag_result)

    # -- Verification dashboard ------------------------------------------------

    def _get_cache_summary() -> dict[str, Any]:
        """Return verification cache summary. Closure-scoped accessor."""
        verified: list[str] = []
        failed: list[str] = []
        seen: set[str] = set()
        for key, entry in _verify_cache.items():
            path = key[0] if isinstance(key, tuple) else str(key)
            if path in seen:
                continue
            seen.add(path)
            if entry.result.get("success"):
                verified.append(path)
            else:
                failed.append(path)
        return {
            "verified_files": verified,
            "failed_files": failed,
            "cache_size": len(_verify_cache),
            "cache_max": _CACHE_MAX_SIZE,
        }

    # Expose cache summary via ctx for monitoring/dashboard use
    ctx.get_verify_cache_summary = _get_cache_summary

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_verification_dashboard() -> dict:
        """Workspace-level verification status: files verified, failed, pending.

        Returns verification cache state showing which files have been
        verified, which failed, and which are pending.
        """
        logger.debug("[ivy_verification_dashboard] workspace=%s", ctx.root)
        _tc = ToolTraceContext("ivy_verification_dashboard", {})
        ivy_files = ctx.find_ivy_files(ctx.root)
        cache = _get_cache_summary()
        verified_set = set(cache["verified_files"])
        failed_set = set(cache["failed_files"])
        pending = [
            f for f in ivy_files if f not in verified_set and f not in failed_set
        ]

        return _tc.finish(
            {
                "success": True,
                "total_files": len(ivy_files),
                "verified": len(verified_set),
                "failed": len(failed_set),
                "pending": len(pending),
                "cache_size": cache["cache_size"],
                "cache_max": cache["cache_max"],
                "verified_files": sorted(verified_set),
                "failed_files": sorted(failed_set),
            }
        )
