"""Custom Ivy tool commands: verify, compile, show model."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from lsprotocol import types as lsp

from ivy_lsp.core.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import LogCategory, LogEvent, StructuredLogAdapter
from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.infra.utils.validation import validate_ivy_param as _validate_ivy_param
from ivy_lsp.lsp.commands_helpers import (
    _collect_all_isolates,
    _detect_isolate_at_position,
    _detect_isolate_from_params,
    _find_enclosing_test,
    _find_tool,
    _get_compile_env,
    _redirect_to_enclosing_test,
    _refresh_open_diagnostics_async,
    _refresh_open_diagnostics_sync,
    _resolve_via_staging,
    _run_tool,
    _track_end,
    _track_error,
    _track_start,
)

logger = logging.getLogger(__name__)
slog = StructuredLogAdapter(logger, {})


def register(server: Any) -> None:
    """Register custom Ivy command handlers."""

    @server.feature("ivy/verify")
    async def ivy_verify(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)

        isolate, _ = _detect_isolate_from_params(server, uri, params)

        # Redirect module files to their enclosing test
        filepath, isolate, redirected = _redirect_to_enclosing_test(
            server, uri, filepath, isolate, "verify"
        )

        op_id = _track_start(server, "verify", filepath)
        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_check"]
        if isolate:
            cmd.append(f"isolate={_validate_ivy_param(isolate)}")
        elif redirected:
            cmd.append("coi=false")
        cmd.append(staged_filepath)

        try:
            result = await _run_tool(
                cmd,
                get_config().verify_timeout,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
            )
            result["isolate"] = isolate

            # Parse output into diagnostics
            from ivy_lsp.lsp.diagnostics.compute import parse_ivy_check_output

            combined = "\n".join(result["output"])
            deep_diags = parse_ivy_check_output(combined)
            result["diagnosticCount"] = len(deep_diags)

            # Publish merged diagnostics
            from ivy_lsp.lsp.diagnostics.compute import compute_diagnostics

            doc = server.workspace.get_text_document(uri)
            base_diags = compute_diagnostics(
                server.parser, doc.source or "", filepath, server.indexer
            )
            server.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(
                    uri=uri, version=doc.version, diagnostics=base_diags + deep_diags
                )
            )

            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/compile")
    async def ivy_compile(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)
        target = getattr(params, "target", "test")

        # Redirect module files to their enclosing test
        enclosing_test = _find_enclosing_test(server, filepath)
        if enclosing_test is not None:
            slog.info(
                "Redirecting ivyc from module %s to test %s",
                filepath,
                enclosing_test,
                extra={
                    "event": LogEvent(
                        LogCategory.ACTIVITY,
                        "compile",
                        {"module": filepath, "test": enclosing_test},
                    )
                },
            )
            filepath = enclosing_test

        op_id = _track_start(server, "compile", filepath)

        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = [
            "ivyc",
            f"target={_validate_ivy_param(target)}",
            os.path.basename(staged_filepath),
        ]
        try:
            result = await _run_tool(
                cmd,
                get_config().tool_compile_timeout,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
                env=_get_compile_env(),
            )
            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/showModel")
    async def ivy_show_model(params) -> Dict[str, Any]:
        uri = params.textDocument.uri
        filepath = uri_to_path(uri)
        token = getattr(params, "workDoneToken", None)

        isolate, _ = _detect_isolate_from_params(server, uri, params)

        # If still no isolate, collect from file + transitive includes
        if isolate is None:
            doc = server.workspace.get_text_document(uri)
            source = doc.source or ""
            all_isolates = _collect_all_isolates(server, filepath, source)
            if len(all_isolates) == 1:
                isolate = all_isolates[0]
            elif len(all_isolates) > 1:
                return {
                    "success": False,
                    "message": "Multiple isolates found — please select one",
                    "output": [],
                    "duration": 0.0,
                    "availableIsolates": all_isolates,
                }

        # Redirect module files to their enclosing test to avoid
        # circular-include "redefining" errors (mirrors ivy_to_cpp.py).
        filepath, isolate, redirected = _redirect_to_enclosing_test(
            server, uri, filepath, isolate, "show_model"
        )

        op_id = _track_start(server, "showModel", filepath)
        staged_filepath = _resolve_via_staging(server, filepath)
        cmd = ["ivy_show"]
        if isolate:
            cmd.append(f"isolate={_validate_ivy_param(isolate)}")
        elif redirected:
            cmd.append("coi=false")
        cmd.append(staged_filepath)
        try:
            result = await _run_tool(
                cmd,
                get_config().show_model_timeout,
                server,
                token,
                cwd=os.path.dirname(staged_filepath),
            )
            result["isolate"] = isolate
            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    @server.feature("ivy/capabilities")
    async def ivy_capabilities(params: Any = None) -> Dict[str, Any]:
        return {
            "fullMode": getattr(server, "full_mode", False),
            "ivyCheckAvailable": _find_tool("ivy_check") is not None,
            "ivycAvailable": _find_tool("ivyc") is not None,
            "ivyShowAvailable": _find_tool("ivy_show") is not None,
            "compiledModelAvailable": getattr(server, "compiler_manager", None)
            is not None,
        }

    @server.feature("ivy/setActiveTest")
    async def ivy_set_active_test(params: Any = None) -> Dict[str, Any]:
        """Set the active test scope for diagnostics and code lenses.

        On success, re-publishes diagnostics for all open documents
        so scoped filtering takes effect immediately.
        """
        if params is None:
            return {"success": False, "error": "No params provided"}

        test_file = getattr(params, "testFile", None)

        try:
            graph = server.indexer.requirement_graph
        except AttributeError:
            return {"success": False, "error": "Indexer not available"}

        if not isinstance(graph, ScopedRequirementModel):
            return {"success": False, "error": "Scoped model not available"}

        if test_file is not None and not graph.has_test_scope(test_file):
            active = graph.get_active_scope()
            return {
                "success": False,
                "error": f"Unknown test: {test_file}",
                "activeTest": active.test_file if active else None,
            }

        graph.set_active_test(test_file)
        active = graph.get_active_scope()

        await _refresh_open_diagnostics_async(server)

        return {
            "success": True,
            "activeTest": active.test_file if active else None,
        }

    @server.feature("ivy/listTests")
    async def ivy_list_tests(params: Any = None) -> Dict[str, Any]:
        """List all discovered test scopes with metadata."""
        try:
            graph = server.indexer.requirement_graph
        except AttributeError:
            return {"tests": [], "activeTest": None}

        if not isinstance(graph, ScopedRequirementModel):
            return {"tests": [], "activeTest": None}

        tests = []
        for _test_file, scope in graph.iter_test_scopes():
            tests.append(
                {
                    "testFile": scope.test_file,
                    "testerRole": scope.tester_role,
                    "exportCount": len(scope.exported_actions),
                    "importCount": len(scope.imported_actions),
                    "includeCount": len(scope.include_closure),
                }
            )

        active = graph.get_active_scope()
        return {
            "tests": tests,
            "activeTest": active.test_file if active else None,
        }

    @server.feature("ivy/compileTest")
    async def ivy_compile_test(params: Any = None) -> Dict[str, Any]:
        """Compile a specific test file with ivyc target=test."""
        if params is None:
            return {
                "success": False,
                "message": "No params provided",
                "output": [],
                "duration": 0.0,
            }

        test_file = getattr(params, "testFile", None)
        if not test_file:
            return {
                "success": False,
                "message": "No testFile specified",
                "output": [],
                "duration": 0.0,
            }

        op_id = _track_start(server, "compileTest", test_file)
        token = getattr(params, "workDoneToken", None)
        staged = _resolve_via_staging(server, test_file)
        cmd = ["ivyc", "target=test", os.path.basename(staged)]
        try:
            result = await _run_tool(
                cmd,
                get_config().tool_compile_timeout,
                server,
                token,
                env=_get_compile_env(),
                cwd=os.path.dirname(staged),
            )

            # Store compilation result in scoped model if available
            try:
                graph = server.indexer.requirement_graph
                if isinstance(graph, ScopedRequirementModel):
                    graph.set_compilation_result(test_file, result)
            except AttributeError:
                logger.warning(
                    "Failed to store compilation result for %s",
                    test_file,
                    exc_info=True,
                )

            _track_end(server, op_id, result)
            return result
        except Exception as e:
            _track_error(server, op_id, e)
            raise

    # Extended commands (compiledModel, activeDocumentChanged, code-lens
    # handlers, recompileAll) are registered via commands_extended.py.
    from ivy_lsp.lsp.commands_extended import register_extended_commands

    register_extended_commands(server)
