"""Verification tools: ivy_verify, ivy_compile, ivy_model_info."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any

from ivy_lsp.core.verification import run_ivy_check as shared_ivy_check
from ivy_lsp.core.verification import run_ivy_compile as shared_ivy_compile
from ivy_lsp.core.verification import run_ivy_show as shared_ivy_show
from ivy_lsp.infra.observability import trace_tool
from ivy_lsp.infra.utils.ivy_output import extract_error_summary, parse_ivy_output
from ivy_lsp.infra.utils.validation import validate_ivy_param as _validate_ivy_param
from ivy_lsp.mcp.tools import error_response, inject_scope_metadata, safe_tool
from ivy_lsp.mcp.tools._helpers import resolve_scope, validated_path_or_error
from ivy_lsp.mcp.tools.verification_cache import (
    CacheEntry,
    cache_is_fresh,
    cache_per_isolate_results,
    create_cache,
    evict_oldest,
    get_cache_summary,
    get_file_mtime,
    get_include_mtimes,
)

logger = logging.getLogger(__name__)


def register_verification_tools(mcp: Any, ctx: Any) -> None:
    """Register verification-related MCP tools."""
    _verify_cache, _verify_cache_lock, _verify_in_flight = create_cache()

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_verify(
        relative_path: str,
        isolate: str | None = None,
        use_cache: bool = False,
        compact: bool = True,
        scope: str = "",
        timeout: float = 120.0,
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
            timeout: Maximum seconds to wait for ivy_check (default 120).
                Complex models with many isolates may need longer timeouts.
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

        _resolved_scope = resolve_scope(ctx, scope, "ivy_verify")

        with trace_tool(
            "ivy_verify",
            {
                "relative_path": relative_path,
                "isolate": isolate,
                "use_cache": use_cache,
                "scope": scope,
            },
        ) as _tt:
            abs_path, err = validated_path_or_error(ctx, relative_path)
            if err:
                _tt[0] = err
                return err
            assert abs_path is not None
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
                    if cache_is_fresh(entry, abs_path):
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
                            if cache_is_fresh(entry, abs_path):
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
                    timeout=timeout,
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
                    _verify_cache[cache_key] = CacheEntry(
                        result=dict(result),
                        file_mtime=get_file_mtime(abs_path),
                        include_mtimes=get_include_mtimes(
                            abs_path,
                            lambda name: ctx.get_basename_cache().get(name, []),
                        ),
                    )
                    evict_oldest(_verify_cache)

                    if isolate is None:
                        raw_output = result.get("raw_output", "")
                        cache_per_isolate_results(
                            _verify_cache, abs_path, raw_output, result
                        )

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
        if not shutil.which("ivyc") and ctx.executor is None:
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

        _resolved_scope = resolve_scope(ctx, scope, "ivy_compile")
        with trace_tool(
            "ivy_compile",
            {"relative_path": relative_path, "target": target, "isolate": isolate},
        ) as _tt:
            abs_path, err = validated_path_or_error(ctx, relative_path)
            if err:
                _tt[0] = err
                return err
            assert abs_path is not None
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

                    from panther_ivy.api.compiler import (  # type: ignore[import-not-found]
                        generate_compile_commands,
                    )

                    compile_result = generate_compile_commands(
                        ivy_file=P(abs_path),
                        base_path=ctx.base_path,
                    )

                    start = time.monotonic()
                    loop = asyncio.get_running_loop()
                    setup_result = await loop.run_in_executor(
                        ctx.tool_executor,
                        lambda: ctx.executor.execute(
                            compile_result.setup_commands,
                            workspace_root=ctx.root,
                            timeout=30,
                        ),
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
                    exec_result = await loop.run_in_executor(
                        ctx.tool_executor,
                        lambda: ctx.executor.execute(
                            compile_result.compile_commands,
                            workspace_root=ctx.root,
                            timeout=300,
                        ),
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
            abs_path, err = validated_path_or_error(ctx, relative_path)
            if err:
                _tt[0] = err
                return err
            assert abs_path is not None
            if not os.path.isfile(abs_path):
                _tt[0] = error_response(f"File not found: {relative_path}")
                return _tt[0]

            if isolate:
                try:
                    _validate_ivy_param(isolate)
                except ValueError as exc:
                    _tt[0] = error_response(str(exc))
                    return _tt[0]

            # Auto-redirect: if no isolate specified, try finding a test
            # entry point that includes this file so ivy_show has context.
            effective_path = abs_path
            if not isolate:
                try:
                    graph = await ctx.get_req_graph()
                    if graph is not None and hasattr(graph, "get_tests_for_file"):
                        tests = sorted(graph.get_tests_for_file(abs_path))
                        if tests:
                            effective_path = tests[0]
                            logger.debug(
                                "[ivy_model_info] auto-redirected %s → %s",
                                os.path.basename(abs_path),
                                os.path.basename(effective_path),
                            )
                except Exception:
                    pass

            # When no isolate is specified, disable cone-of-influence
            # to match ivyc behavior.  ivy_show (unlike ivy_check) does
            # not iterate over isolates itself, so it fails with
            # "no isolate specified on command line" when the compiled
            # module contains isolates and coi is enabled.
            result = await shared_ivy_show(
                filepath=effective_path,
                workspace_root=ctx.root,
                isolate=isolate,
                staging_dir=ctx.staging_dir,
                resolver=ctx.include_resolver,
                coi=bool(isolate),
            )

            # If redirected, note the original file in the result
            if effective_path != abs_path:
                result["redirected_from"] = relative_path
                result["redirected_to"] = os.path.relpath(effective_path, ctx.root)

            _tt[0] = result
            return _tt[0]

    from ivy_lsp.mcp.tools.diagnostics_tool import register_diagnostic_tools

    def _cache_summary() -> dict:
        return get_cache_summary(_verify_cache)

    ctx.get_verify_cache_summary = _cache_summary
    register_diagnostic_tools(mcp, ctx, _cache_summary)
