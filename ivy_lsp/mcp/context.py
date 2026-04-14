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


class _StagingDirDescriptor:
    """Descriptor that reads staging_dir live from the include resolver.

    When accessed, checks (in order):
    1. The live ``_staging_dir`` from the include resolver (via
       ``include_resolver`` descriptor or ``_lsp_server_ref``).
    2. A directly assigned snapshot (set via ``ctx.staging_dir = val``).
    3. None.

    This ensures the sidecar picks up rebuilt staging directories after
    workspace switches or ``ivy_index`` runs.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_{name}_snapshot"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        resolver = None
        direct_resolver = getattr(obj, "_include_resolver_value", None)
        if direct_resolver is not None:
            resolver = direct_resolver
        else:
            server = getattr(obj, "_lsp_server_ref", None)
            if server is not None:
                indexer = getattr(server, "_indexer", None)
                if indexer is not None:
                    resolver = getattr(indexer, "resolver", None)
        if resolver is not None:
            live = getattr(resolver, "_staging_dir", None)
            if live is not None:
                return live
        return getattr(obj, self._attr, None)

    def __set__(self, obj: Any, value: Any) -> None:
        if isinstance(value, _StagingDirDescriptor):
            value = None
        setattr(obj, self._attr, value)


class _IncludeResolverDescriptor:
    """Descriptor that provides lazy resolution for include_resolver.

    When accessed, checks (in order):
    1. A directly assigned resolver (set via ``ctx.include_resolver = r``).
    2. The live resolver from an LSP server reference (``ctx._lsp_server_ref``).
    3. None.

    This allows the sidecar's ToolContext to pick up the LSP indexer's
    resolver even when the indexer initializes after the sidecar starts.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_{name}_value"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        direct = getattr(obj, self._attr, None)
        if direct is not None:
            return direct
        server = getattr(obj, "_lsp_server_ref", None)
        if server is not None:
            indexer = getattr(server, "_indexer", None)
            if indexer is not None:
                return getattr(indexer, "resolver", None)
        return None

    def __set__(self, obj: Any, value: Any) -> None:
        if isinstance(value, _IncludeResolverDescriptor):
            value = None
        setattr(obj, self._attr, value)


@dataclass
class ToolContext:
    """Shared context passed to every tool registration module.

    Holds workspace state, lazy builders, and helper methods that were
    previously closure-captured inside ``start_mcp()``.
    """

    root: str
    executor: Any
    base_path: str | None = None
    staging_dir: str | None = _StagingDirDescriptor()  # type: ignore[assignment]

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
    include_resolver: Any = _IncludeResolverDescriptor()  # type: ignore[assignment]
    _basename_cache_invalidate: Callable[[], None] = field(default=lambda: None)
    invalidate_caches: Callable[[], None] = field(default=lambda: None)

    # Dedicated thread pool for tool-originated blocking calls.
    # Isolates tool execution from the default pool used by model/graph builders,
    # preventing thread pool starvation during heavy background compilation.
    tool_executor: concurrent.futures.ThreadPoolExecutor | None = None

    # Active workspace management
    active_workspace: Any = None  # Optional[ActiveWorkspace]
    workspace_groups: dict = field(default_factory=dict)  # From .ivyworkspace

    # Workspace context (loaded from .ivy-index/, shared with LSP)
    workspace_context: Any = None

    # LSP server reference for lazy resolver lookup (sidecar path only).
    # When set, the include_resolver descriptor can read the live indexer's
    # resolver even if the indexer wasn't ready when this context was created.
    _lsp_server_ref: Any = field(default=None, init=False, repr=False)

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
                            if ftl.get(v) in active
                            or ftl.get(os.path.realpath(v)) in active
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

        # Sidecar starts ~28s before the indexer is ready, so we must
        # read server._indexer lazily rather than snapshotting at init.
        _last_indexer_id: list[int | None] = [id(server._indexer)]
        _last_resolver_id: list[int | None] = [None]

        def _get_live_indexer():
            return getattr(server, "_indexer", None)

        def _get_live_resolver():
            idx = _get_live_indexer()
            return idx.resolver if idx is not None else None

        def _get_live_ws_root() -> str:
            idx = _get_live_indexer()
            if idx is not None:
                return getattr(idx, "_workspace_root", "") or ""
            return ""

        def _get_live_exclude():
            r = _get_live_resolver()
            base = DEFAULT_EXCLUDE_DIRS
            if r is not None and hasattr(r, "_exclude_paths"):
                base = base | frozenset(r._exclude_paths)
            return base

        def _find_files(search_root: str) -> list[str]:
            r = _get_live_resolver()
            if r is not None:
                has_active_ws = bool(getattr(r, "_active_layers", None))
                return r.find_all_ivy_files(filter_active=has_active_ws)
            root = _get_live_ws_root() or search_root
            return _find_ivy_raw(root, _get_live_exclude()) if root else []

        _basename_cache_obj = BasenameCache(_find_files, "")

        def _get_basename_cache() -> dict[str, list[str]]:
            idx_id = id(_get_live_indexer())
            res_id = id(_get_live_resolver())
            if idx_id != _last_indexer_id[0] or res_id != _last_resolver_id[0]:
                _last_indexer_id[0] = idx_id
                _last_resolver_id[0] = res_id
                _basename_cache_obj._root = _get_live_ws_root()
                _basename_cache_obj.invalidate()
            return _basename_cache_obj.get()

        # Initial workspace root — may be empty if indexer not ready yet
        ws_root = _get_live_ws_root()

        discovered_stdlib = discover_stdlib_modules()

        # staging_dir=None: the _StagingDirDescriptor reads live from
        # include_resolver, which reads from _lsp_server_ref.
        ctx = cls(
            root=ws_root,
            staging_dir=None,
            executor=None,
            base_path=None,
            stdlib_modules=discovered_stdlib,
        )

        # Wire up callables backed by the LSP server's live state
        ctx.find_ivy_files = _find_files
        ctx._lsp_server_ref = server
        # Load workspace context from offline .ivy-index/ artifacts.
        # The standalone MCP path loads this via a background prewarm
        # thread (server.py:_prewarm_fn), but the sidecar runs inside the
        # LSP process and must load it directly.  WorkspaceContext.load()
        # reads manifest JSON from disk — fast enough for synchronous use.
        _ws_ctx = getattr(server, "_workspace_context", None)
        if _ws_ctx is None and ws_root:
            try:
                from ivy_lsp.core.workspace.context import WorkspaceContext

                _ws_ctx = WorkspaceContext.load(ws_root)
            except Exception:
                pass
        ctx.workspace_context = _ws_ctx

        async def _get_model():
            return server._semantic_model

        async def _get_req_graph():
            idx = _get_live_indexer()
            if idx is not None:
                return idx.requirement_graph
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
                workspace_root=_get_live_ws_root() or ctx.root,
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
