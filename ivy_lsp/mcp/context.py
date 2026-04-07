"""ToolContext dataclass and path/structural-lint helpers.

Extracted from ``ivy_lsp.mcp.server`` (Phase 5a) so that the context
object can be imported without pulling in the full MCP server machinery.
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from ivy_lsp.core.structural_lint import (
    check_structural_issues_raw,
    check_unresolved_includes_raw,
)
from ivy_lsp.infra.utils.basename_cache import BasenameCache
from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS
from ivy_lsp.infra.utils.ivy_output import (  # noqa: F401
    find_ivy_files as _find_ivy_files_raw,
)


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
        alt = os.path.realpath(os.path.join(root, "protocol-testing", relative_path))
        if alt.startswith(real_root + os.sep) and os.path.exists(alt):
            return alt

    return abs_path


def _check_structural_issues(
    source: str,
    filepath: str,
    resolve_callback: Any = None,
) -> list[dict[str, Any]]:
    """Fast structural checks without full parsing."""
    diags = check_structural_issues_raw(source, filepath)
    diags.extend(check_unresolved_includes_raw(source, filepath, resolve_callback))
    return diags


@dataclass
class ToolContext:
    """Shared context passed to every tool registration module.

    Holds workspace state, lazy builders, and helper methods that were
    previously closure-captured inside ``start_mcp()``.
    """

    root: str
    staging_dir: str | None
    executor: Any
    base_path: str | None

    # Callable helpers — assigned after construction inside start_mcp()
    find_ivy_files: Callable[..., list[str]] = field(default=lambda: [])
    get_model: Callable[..., Any] = field(default=lambda: None)
    get_model_or_none: Callable[..., Any] = field(default=lambda: None)
    get_model_status: Callable[..., dict] = field(
        default=lambda: {"state": "not_built"}
    )
    get_req_graph: Callable[..., Any] = field(default=lambda: None)
    make_viz_server_proxy: Callable[..., Any] = field(default=lambda: None)
    get_basename_cache: Callable[..., dict[str, list[str]]] = field(default=lambda: {})
    make_resolve_callback: Callable[..., Any] = field(default=lambda: None)
    include_resolver: Any = None
    _basename_cache_invalidate: Callable[[], None] = field(default=lambda: None)

    # Dedicated thread pool for tool-originated blocking calls.
    # Isolates tool execution from the default pool used by model/graph builders,
    # preventing thread pool starvation during heavy background compilation.
    tool_executor: concurrent.futures.ThreadPoolExecutor | None = None

    # Active workspace management
    active_workspace: Any = None  # Optional[ActiveWorkspace]
    workspace_groups: dict = field(default_factory=dict)  # From .ivyworkspace

    # Workspace context (loaded from .ivy-index/, shared with LSP)
    workspace_context: Any = None

    # Known Ivy standard library modules (fallback; overwritten at runtime
    # by discover_stdlib_modules() which scans ivy/include/1.7/)
    stdlib_modules: frozenset[str] = frozenset(
        {
            "order",
            "collections",
            "collections_impl",
            "ip",
            "ipv6",
            "tcp",
            "tcp_impl",
            "udp",
            "udp_impl",
            "byte_stream",
            "timeout",
            "net",
            "tls",
            "tls_msg",
            "serdes",
            "deserializer",
            "c_time",
            "chrono_time",
        }
    )

    def build_context_metadata(self) -> dict:
        """Build workspace/scope context metadata for tool result injection.

        Returns empty dict when workspace is not set.
        """
        ctx: dict = {}
        ws = self.active_workspace
        if ws is None or not getattr(ws, "active_group", None):
            return ctx
        ctx["workspace"] = ws.active_group
        ctx["layers"] = sorted(ws.active_layers)
        ctx["set_by"] = getattr(ws, "set_by", "unknown")
        resolver = self.include_resolver
        if resolver is not None and hasattr(resolver, "_file_to_layer"):
            ftl = resolver._file_to_layer
            active = getattr(resolver, "_active_layers", set())
            if active:
                ctx["files_in_scope"] = sum(
                    1 for layer in ftl.values() if layer in active
                )
            else:
                ctx["files_in_scope"] = len(ftl)
            ctx["files_total"] = len(ftl)
            # Filtered collision count
            cmap = getattr(resolver, "_collision_map", {})
            if cmap:
                if active:
                    in_scope_collisions = sum(
                        1
                        for variants in cmap.values()
                        if sum(
                            1
                            for v in variants
                            if ftl.get(os.path.realpath(v)) in active
                        )
                        > 1
                    )
                else:
                    in_scope_collisions = len(cmap)
                ctx["collisions_in_scope"] = in_scope_collisions
                ctx["collisions_total"] = len(cmap)
        return ctx

    @classmethod
    def from_lsp_server(cls, server: Any) -> "ToolContext":
        """Bridge an IvyLanguageServer instance into a ToolContext.

        Maps the LSP server's live state (indexer, semantic model,
        requirement graph, resolver) into the ToolContext interface
        so MCP tools can share the same data without re-indexing.

        Handles ``server._indexer is None`` gracefully — tools that
        need the model/graph already handle None returns.
        """
        from ivy_lsp.core.indexer.include_resolver import discover_stdlib_modules
        from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS
        from ivy_lsp.infra.utils.ivy_output import find_ivy_files as _find_ivy_raw

        indexer = server._indexer
        resolver = indexer.resolver if indexer is not None else None
        ws_root = indexer._workspace_root if indexer is not None else ""

        staging_dir = None
        if resolver is not None and hasattr(resolver, "_staging_dir"):
            staging_dir = resolver._staging_dir

        # Build file finder that delegates to the resolver
        _exclude = DEFAULT_EXCLUDE_DIRS
        if resolver is not None and hasattr(resolver, "_exclude_paths"):
            _exclude = _exclude | frozenset(resolver._exclude_paths)

        def _find_files(search_root: str) -> list[str]:
            if resolver is not None:
                has_active_ws = bool(getattr(resolver, "_active_layers", None))
                return resolver.find_all_ivy_files(filter_active=has_active_ws)
            return _find_ivy_raw(search_root, _exclude)

        # Basename cache
        _basename_cache_obj = BasenameCache(_find_files, ws_root)
        _get_basename_cache = _basename_cache_obj.get

        discovered_stdlib = discover_stdlib_modules()

        ctx = cls(
            root=ws_root,
            staging_dir=staging_dir,
            executor=None,
            base_path=None,
            stdlib_modules=discovered_stdlib,
        )

        # Wire up callables backed by the LSP server's live state
        ctx.find_ivy_files = _find_files
        ctx.include_resolver = resolver
        ctx.workspace_context = getattr(server, "_workspace_context", None)

        async def _get_model():
            return server._semantic_model

        async def _get_req_graph():
            if indexer is not None:
                return indexer.requirement_graph
            return None

        def _get_model_status() -> dict:
            if server._semantic_model is not None:
                return {"state": "ready"}
            if server._initializing:
                return {"state": "building"}
            # _setup_analysis_pipeline completed (pipeline exists) but
            # _semantic_model is still None — treat as ready with empty
            # model rather than "not_built", which blocks all tools.
            if getattr(server, "_analysis_pipeline", None) is not None:
                return {"state": "ready"}
            return {"state": "not_built"}

        ctx.get_model = _get_model
        ctx.get_model_status = _get_model_status
        ctx.get_req_graph = _get_req_graph
        ctx.get_basename_cache = _get_basename_cache
        # Wire cache invalidation callback for workspace switching
        ctx._basename_cache_invalidate = _basename_cache_obj.invalidate

        def _make_resolve_callback():
            cache = _get_basename_cache()

            def _resolve(inc_name: str, from_file: str) -> str | None:
                if inc_name in ctx.stdlib_modules:
                    return f"<stdlib>/{inc_name}.ivy"
                candidates = cache.get(inc_name)
                if candidates:
                    return candidates[0]
                return None

            return _resolve

        ctx.make_resolve_callback = _make_resolve_callback

        # Visualization server proxy (canonical definitions in server.py)
        from ivy_lsp.mcp.server import _IndexerProxy, _ServerProxy

        async def _make_viz_server_proxy():
            graph = await _get_req_graph()
            return _ServerProxy(
                indexer=_IndexerProxy(requirement_graph=graph),
                workspace_root=ws_root,
            )

        ctx.make_viz_server_proxy = _make_viz_server_proxy

        return ctx

    def validate_path(self, relative_path: str) -> str:
        """Resolve *relative_path* under workspace root."""
        return _validate_path(self.root, relative_path)

    def check_structural_issues(
        self,
        source: str,
        filepath: str,
        resolve_callback: Any = None,
    ) -> list[dict[str, Any]]:
        """Fast structural checks without full parsing."""
        return _check_structural_issues(source, filepath, resolve_callback)
