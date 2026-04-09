"""Shared helpers for MCP tool implementations."""

from __future__ import annotations

import logging
import os
from typing import Any

from ivy_lsp.mcp.tools import error_response

logger = logging.getLogger(__name__)


def validated_path_or_error(ctx: Any, path: str) -> tuple[str | None, dict | None]:
    """Validate a path against the workspace root.

    Returns ``(abs_path, None)`` on success, or ``(None, error_dict)``
    when validation fails.
    """
    try:
        return ctx.validate_path(path), None
    except ValueError as exc:
        return None, error_response(str(exc))


def build_viz_params(
    ctx: Any,
    *,
    file_path: str | None = None,
    test_file: str | None = None,
    protocol: str | None = None,
) -> tuple[dict[str, Any], dict | None]:
    """Build a params dict for visualization/quality handlers.

    Validates path arguments and adds protocol filter.  Returns
    ``(params, None)`` on success, or ``(params, error_dict)`` on the
    first validation failure.
    """
    params: dict[str, Any] = {}
    if file_path:
        abs_path, err = validated_path_or_error(ctx, file_path)
        if err:
            return params, err
        params["filePath"] = abs_path
    if test_file:
        abs_path, err = validated_path_or_error(ctx, test_file)
        if err:
            return params, err
        params["testFile"] = abs_path
    if protocol:
        params["protocolFilter"] = f"protocol-testing/{protocol}/"
    return params, None


def model_unavailable_response(ctx: Any) -> dict:
    """Build a rich error response based on the model's build state."""
    status = ctx.get_model_status()
    if status.get("state") == "not_built":
        return {
            "success": False,
            "message": "Semantic model unavailable",
            "note": "LSP is still indexing. Results may be incomplete. Try again shortly.",
        }
    if status.get("state") == "building":
        return {
            "success": False,
            "message": "Semantic model is currently building",
            "note": (
                "The model is being built (this can take 2-4 minutes on first use). "
                "Try again shortly."
            ),
        }
    if status.get("state") == "failed":
        return {
            "success": False,
            "message": "Semantic model unavailable",
            "note": (
                f"Model build failed: {status.get('error', 'unknown')}. "
                f"Retry in {status.get('retry_in_seconds', '?')}s."
            ),
        }
    return error_response("Semantic model unavailable")


async def get_model_or_error(ctx: Any) -> tuple[Any | None, dict | None]:
    """Get the semantic model, or return an error response.

    Returns ``(model, None)`` on success, or ``(None, error_dict)`` on failure.
    """
    status = ctx.get_model_status()
    if status.get("state") not in ("ready", "not_built"):
        return None, model_unavailable_response(ctx)
    model = await ctx.get_model()
    if model is None:
        return None, model_unavailable_response(ctx)
    return model, None


async def get_model_if_ready(ctx: Any) -> Any | None:
    """Get the semantic model if available, else ``None`` (no error response)."""
    status = ctx.get_model_status()
    if status.get("state") not in ("ready", "not_built"):
        return None
    return await ctx.get_model()


def load_requirements_from_manifests(root: str) -> list:
    """Load requirements from all manifest files in a workspace root."""
    from ivy_lsp.core.semantic.rfc_annotations import (
        find_manifests,
        load_requirement_manifest,
    )

    reqs: list = []
    for path in find_manifests(root):
        reqs.extend(load_requirement_manifest(path).values())
    return reqs


async def apply_scope_filter(
    items: list,
    *,
    test_file: str | None = None,
    relative_path: str | None = None,
    ctx: Any,
    file_attr: str = "file",
) -> list | dict:
    """Filter items by test-file scope or relative path.

    Returns the filtered list on success, or an ``error_response`` dict
    if path validation fails.
    """
    if test_file:
        try:
            abs_test = ctx.validate_path(test_file)
        except ValueError as exc:
            return error_response(str(exc))
        graph = await ctx.get_req_graph()
        if graph is not None:
            scope = graph.get_test_scope(abs_test)
            if scope is not None:
                scope_files = scope.include_closure
                return [a for a in items if getattr(a, file_attr) in scope_files]
            return [a for a in items if getattr(a, file_attr) == abs_test]
        return [a for a in items if getattr(a, file_attr) == abs_test]
    if relative_path:
        try:
            abs_path = ctx.validate_path(relative_path)
        except ValueError as exc:
            return error_response(str(exc))
        if os.path.isdir(abs_path):
            prefix = abs_path.rstrip(os.sep) + os.sep
            return [a for a in items if getattr(a, file_attr).startswith(prefix)]
        return [a for a in items if getattr(a, file_attr) == abs_path]
    return items


def resolve_scope(ctx: Any, scope: str, tool_name: str) -> Any | None:
    """Resolve scope and log warning if unknown.

    Returns the resolved scope object, or None.
    Replaces 7+ repeated scope resolution blocks across tool modules.
    """
    if not scope or getattr(ctx, "workspace_context", None) is None:
        return None
    resolved = ctx.workspace_context.get_test_scope(scope)
    if resolved is None:
        logger.warning(
            "[%s] Unknown scope '%s'; proceeding without scoping",
            tool_name,
            scope,
        )
    return resolved
