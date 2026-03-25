"""Extended command handlers extracted from commands.py.

Contains handlers for:
- ivy/compiledModel       -- cached compilation IR as JSON
- ivy/activeDocumentChanged -- auto-detect active test on document switch
- ivy.showActionRequirements -- code-lens: monitor/state-var details
- ivy.showPropertyDetails    -- code-lens: property/axiom/invariant details
- ivy.navigateToInclude      -- code-lens: include directive navigation
- ivy.showRfcDetails         -- code-lens: RFC tag details
- ivy.noop                   -- code-lens: informational no-op
- ivy/recompileAll           -- bulk recompilation of all test entry points
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

from ivy_lsp.analysis.test_scope import ScopedRequirementModel
from ivy_lsp.features.commands import (
    _extract_param,
    _refresh_open_diagnostics_async,
    _ServerProxy,
)
from ivy_lsp.utils import uri_to_path

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Handler implementations (module-level async functions)
# ------------------------------------------------------------------


async def _handle_compiled_model(server: Any, params: Any = None) -> Dict[str, Any]:
    """Return the cached CompiledModuleIR for a file as JSON."""
    if params is None:
        return {"success": False, "error": "No params provided"}

    uri = getattr(params, "textDocument", None)
    if uri is not None:
        uri = getattr(uri, "uri", uri)
    if not uri:
        uri = getattr(params, "uri", None)
    if not uri:
        return {"success": False, "error": "No file URI provided"}

    filepath = uri_to_path(uri)

    manager = getattr(server, "compiler_manager", None)
    if manager is None:
        return {"success": False, "error": "CompilerManager not available"}

    ir = manager.get_cached(filepath)
    if ir is None:
        return {
            "success": False,
            "error": (f"No cached compilation for {os.path.basename(filepath)}"),
            "hint": "Run ivy/compile or wait for background compilation",
        }

    # Flatten mixins from Dict[str, List[MixinIR]] to a single list
    flat_mixins = []
    for mixin_list in ir.mixins.values():
        for m in mixin_list:
            flat_mixins.append({"mixer": m.mixer, "mixee": m.mixee, "kind": m.kind})

    return {
        "success": True,
        "filepath": filepath,
        "compileDuration": ir.compile_duration,
        "sorts": {
            name: {
                "name": s.name,
                "arity": s.arity,
                "isUninterpreted": s.is_uninterpreted,
                "isEnumerated": s.is_enumerated,
                "constructors": list(s.constructors),
            }
            for name, s in ir.sorts.items()
        },
        "symbols": {
            name: {
                "name": s.name,
                "sortStr": s.sort_str,
                "domainSorts": list(s.domain_sorts),
                "rangeSort": s.range_sort,
                "isRelation": s.is_relation,
                "isDestructor": s.is_destructor,
                "isConstructor": s.is_constructor,
            }
            for name, s in ir.symbols.items()
        },
        "actions": {
            name: {
                "name": a.name,
                "formalParams": list(a.formal_params),
                "formalReturns": list(a.formal_returns),
                "isExported": a.is_exported,
                "isImported": a.is_imported,
            }
            for name, a in ir.actions.items()
        },
        "mixins": flat_mixins,
        "isolates": {
            name: {
                "name": iso.name,
                "verifiedComponents": list(iso.verified_components),
                "presentComponents": list(iso.present_components),
            }
            for name, iso in ir.isolates.items()
        },
        "axiomCount": len(ir.labeled_axioms),
        "conjectureCount": len(ir.labeled_conjectures),
        "requirementCount": len(ir.requirements),
        "errors": list(ir.errors),
    }


async def _handle_active_document_changed(server: Any, params: Any = None) -> None:
    """Auto-detect active test when user switches documents.

    If the newly focused document is a registered test file,
    set it as the active test and refresh diagnostics.
    Non-test files are ignored (sticky behavior).
    """
    uri = getattr(params, "uri", None)
    if not uri or not uri.startswith("file://"):
        return

    filepath = uri_to_path(uri)

    try:
        graph = server.indexer.requirement_graph
    except AttributeError:
        return

    if not isinstance(graph, ScopedRequirementModel):
        return

    if not graph.has_test_scope(filepath):
        return  # Non-test file: keep current scope (sticky)

    # Check if already the active test (avoid redundant refresh)
    current = graph.get_active_scope()
    if current and current.test_file == filepath:
        return

    graph.set_active_test(filepath)
    await _refresh_open_diagnostics_async(server)


# ------------------------------------------------------------------
# Code-lens command handlers
#
# These are plain async functions registered via server.feature() in
# the loop at the end of register_extended_commands().  Using
# server.feature() avoids executeCommandProvider.commands, which
# causes vscode-languageclient v9+ to auto-register VS Code commands,
# conflicting with the client-side handlers in extension.ts.
# ------------------------------------------------------------------


async def _handle_show_action_requirements(
    server: Any, params: Any = None
) -> Dict[str, Any]:
    """Handle clicks on monitor/state-var code lenses."""
    from ivy_lsp.features.visualization import handle_action_requirements

    action_name = _extract_param(params, "actionName")

    viz_params: Dict[str, Any] = {}
    if action_name:
        viz_params["actionName"] = action_name

    indexer = getattr(server, "indexer", None)
    if indexer is None:
        return {"error": "No indexer available"}

    proxy = _ServerProxy(indexer=indexer)
    return handle_action_requirements(proxy, viz_params)


async def _handle_show_property_details(
    server: Any, params: Any = None
) -> Dict[str, Any]:
    """Handle clicks on property/axiom/invariant code lenses."""
    prop_id = _extract_param(params, "propertyId")

    indexer = getattr(server, "indexer", None)
    if indexer is None:
        return {"error": "No indexer available"}

    graph = getattr(indexer, "requirement_graph", None)
    if graph is None or prop_id is None:
        return {"error": "No data available"}

    prop = graph.properties.get(prop_id)
    if prop is None:
        return {"error": f"Property {prop_id!r} not found"}

    return {
        "id": prop.id,
        "kind": prop.kind,
        "file": prop.file,
        "line": prop.line,
        "formulaText": prop.formula_text,
    }


async def _handle_navigate_to_include(
    server: Any, params: Any = None
) -> Dict[str, Any]:
    """Handle clicks on include directive code lenses."""
    include_name = _extract_param(params, "includeName")

    # Extract from_file for include resolution context.
    # Params may arrive flat  [includeName, fromUri]
    #                or nested [[includeName, fromUri]]
    from_file = None
    if isinstance(params, list) and params:
        inner = params[0] if isinstance(params[0], list) else params
        if len(inner) > 1:
            raw = inner[1]
            if isinstance(raw, str):
                from_file = uri_to_path(raw) if raw.startswith("file://") else raw
    elif isinstance(params, dict):
        uri = params.get("uri") or params.get("fromFile")
        if uri:
            from_file = uri_to_path(uri) if uri.startswith("file://") else uri

    if not include_name:
        return {"error": "No include name provided"}

    indexer = getattr(server, "indexer", None)
    if indexer is None:
        return {"error": "No indexer available"}

    resolver = getattr(indexer, "resolver", None)
    if resolver is None:
        return {"error": "No resolver available"}

    if not from_file:
        workspace_root = getattr(indexer, "_workspace_root", "")
        from_file = workspace_root

    resolved = resolver.resolve(include_name, from_file)
    if not resolved:
        return {"error": f"Cannot resolve include {include_name!r}"}

    # Validate resolved path is within workspace root
    workspace_root = getattr(indexer, "_workspace_root", None)
    if workspace_root:
        real_resolved = os.path.realpath(resolved)
        real_root = os.path.realpath(workspace_root)
        if (
            not real_resolved.startswith(real_root + os.sep)
            and real_resolved != real_root
        ):
            logger.warning(
                "Resolved include %r escapes workspace: %s",
                include_name,
                resolved,
            )
            return {"error": "Resolved path escapes workspace boundary"}

    return {"resolved": resolved, "uri": "file://" + resolved}


async def _handle_show_rfc_details(server: Any, params: Any = None) -> Dict[str, Any]:
    """Handle clicks on RFC tag code lenses."""
    tag = _extract_param(params, "tag")

    if not tag:
        return {"error": "No RFC tag provided"}

    model = getattr(server, "semantic_model", None)
    if model is None:
        return {"error": "No semantic model available"}

    node = model.get_node(tag)
    if node is None:
        return {"error": f"RFC tag {tag!r} not found"}

    return {
        "id": node.id,
        "level": getattr(node, "level", None),
        "rfc": getattr(node, "rfc", None),
        "text": getattr(node, "text", None),
    }


async def _handle_noop(server: Any, params: Any = None) -> None:
    """No-op command for informational code lenses."""
    return None


async def _handle_recompile_all(server: Any, params: Any = None) -> Dict[str, Any]:
    """Re-trigger bulk compilation for all test entry points.

    Validates that the pipeline, compiler manager, scoped requirement
    model, and test files are available, and that no bulk compilation
    is already running.  Spawns a daemon thread that calls
    ``AnalysisPipeline.run_bulk_tier3()`` and returns immediately
    with the test file count.
    """
    pipeline = getattr(server, "analysis_pipeline", None)
    if pipeline is None:
        return {"success": False, "error": "Analysis pipeline not initialized"}

    if getattr(pipeline, "_compiler_manager", None) is None:
        return {"success": False, "error": "CompilerManager not available"}

    ps = pipeline.get_pipeline_state()
    if ps.get("bulkCompileRunning", False):
        return {"success": False, "error": "Bulk compilation already running"}

    try:
        graph = server.indexer.requirement_graph
    except AttributeError:
        return {"success": False, "error": "Requirement graph not available"}

    if not isinstance(graph, ScopedRequirementModel):
        return {"success": False, "error": "Scoped model not available"}

    test_files = graph.list_test_files()
    if not test_files:
        return {"success": False, "error": "No test files found"}

    cancel_event = server.bulk_analysis_cancel

    def _run():
        progress_cb = None
        try:
            progress_cb = server._make_progress_callback(
                "Ivy Recompilation",
                "Recompiling {total} test files...",
                "Recompiled {total} test files",
                throttle_seconds=1.0,
            )
        except Exception:
            logger.warning(
                "Could not create progress callback for recompilation; "
                "user will not see progress updates",
                exc_info=True,
            )
        pipeline.run_bulk_tier3(
            test_files,
            progress_callback=progress_cb,
            cancel_event=cancel_event,
        )

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)

    return {
        "success": True,
        "message": f"Recompilation started for {len(test_files)} test files",
        "testFileCount": len(test_files),
    }


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


def register_extended_commands(server: Any) -> None:
    """Register extended Ivy command handlers on *server*.

    Called from :func:`ivy_lsp.features.commands.register` after the
    core commands have been registered.
    """

    @server.feature("ivy/compiledModel")
    async def ivy_compiled_model(params: Any = None) -> Dict[str, Any]:
        return await _handle_compiled_model(server, params)

    @server.feature("ivy/activeDocumentChanged")
    async def ivy_active_document_changed(params: Any = None) -> None:
        return await _handle_active_document_changed(server, params)

    # Code-lens commands — registered via server.feature() to avoid
    # executeCommandProvider.commands conflicts with vscode-languageclient v9+.
    _LENS_COMMANDS: Dict[str, Any] = {
        "ivy.showActionRequirements": lambda p=None: _handle_show_action_requirements(
            server, p
        ),
        "ivy.showPropertyDetails": lambda p=None: _handle_show_property_details(
            server, p
        ),
        "ivy.navigateToInclude": lambda p=None: _handle_navigate_to_include(server, p),
        "ivy.showRfcDetails": lambda p=None: _handle_show_rfc_details(server, p),
        "ivy.noop": lambda p=None: _handle_noop(server, p),
    }

    for _cmd_name, _handler in _LENS_COMMANDS.items():
        server.feature(_cmd_name)(_handler)

    @server.feature("ivy/recompileAll")
    async def ivy_recompile_all(params: Any = None) -> Dict[str, Any]:
        return await _handle_recompile_all(server, params)
