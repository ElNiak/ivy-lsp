"""MCP server for ivy-lsp.

Exposes Ivy verification tools, structured diagnostics, include graph,
and fast lint as MCP tools via the Model Context Protocol. Shares the
same parsing and indexing code as the LSP server.

Usage:
    python -m ivy_lsp --mcp      # Standalone MCP over stdio
    python -m ivy_lsp             # LSP + MCP HTTP sidecar (default)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

# Re-export so that tests patching ``ivy_lsp.mcp.server.shared_ivy_check``
# continue to work after tool handlers moved to ``ivy_lsp.mcp.tools.*``.
from ivy_lsp.core.verification import run_ivy_check as shared_ivy_check  # noqa: F401
from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability.session import workspace_hash
from ivy_lsp.infra.utils.basename_cache import BasenameCache
from ivy_lsp.infra.utils.lazy_builder import LazyAsyncBuilder
from ivy_lsp.mcp import client as sidecar_client
from ivy_lsp.mcp.context import ToolContext
from ivy_lsp.mcp.model_builder import (
    build_mcp_model,
    build_requirement_graph,
    write_model_to_index,
)

logger = logging.getLogger(__name__)

# Note: Symbol/type extraction regex patterns formerly defined here have been
# relocated to ivy_lsp.core.parsing.tiered_extractor (Tier 3 fallback).  The
# _build_model() function now uses TieredExtractor for parser -> lexer -> regex
# cascade.  Include extraction is also handled by TieredExtractor.


from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS
from ivy_lsp.infra.utils.ivy_output import find_ivy_files as _find_ivy_files_raw

# --- Visualization proxy dataclasses (module-level) ---


@dataclass
class _IndexerProxy:
    requirement_graph: Any


@dataclass
class _ServerProxy:
    indexer: _IndexerProxy
    initializing: bool = False
    workspace_root: str = ""  # H3: for relative path output


async def _sidecar_monitor(
    workspace_root: str,
    _poll_interval: float = 2.0,
    _max_iterations: int = 0,
) -> None:
    """Background task: poll for sidecar port file, upgrade when ready.

    Args:
        workspace_root: Our workspace root for validation.
        _poll_interval: Override for testing (default 2s).
        _max_iterations: If >0, exit after N iterations (for testing).
    """
    if os.environ.get("IVY_MCP_DISABLE_UPGRADE") == "1":
        logger.info("[SIDECAR-MONITOR] Upgrade disabled by IVY_MCP_DISABLE_UPGRADE")
        return

    ws_hash = workspace_hash(workspace_root)
    elapsed = 0.0
    poll = _poll_interval
    iteration = 0

    while True:
        await asyncio.sleep(poll)
        elapsed += poll
        iteration += 1

        if _max_iterations > 0 and iteration >= _max_iterations:
            return

        # Progressive backoff: fast for first 60s, then slow
        if elapsed > 60 and poll < 10:
            poll = 10.0
            logger.debug("[SIDECAR-MONITOR] Backing off to 10s polling")

        # Already upgraded — slow heartbeat but re-check port file
        # to detect sidecar restarts (e.g. new LSP on a different port).
        current_port = sidecar_client.get_sidecar_port()
        if current_port is not None:
            poll = 30.0
            new_port = sidecar_client.read_port_file(ws_hash=ws_hash)
            if new_port is not None and new_port != current_port:
                if await sidecar_client.validate_sidecar_workspace(
                    new_port, workspace_root
                ):
                    sidecar_client.set_sidecar_port(new_port)
                    logger.info(
                        "[SIDECAR-MONITOR] Port changed %d -> %d, updated",
                        current_port,
                        new_port,
                    )
                else:
                    logger.debug(
                        "[SIDECAR-MONITOR] Port file says %d but workspace mismatch",
                        new_port,
                    )
            continue

        port = sidecar_client.read_port_file(ws_hash=ws_hash)
        if port is None:
            continue

        if not await sidecar_client.validate_sidecar_workspace(port, workspace_root):
            logger.debug("[SIDECAR-MONITOR] Workspace mismatch, skipping")
            continue

        # Only store the port — actual connection happens in safe_tool's
        # event loop to avoid cross-event-loop ClientSession issues.
        sidecar_client.set_sidecar_port(port)
        logger.info(
            "[SIDECAR-DISCOVERED] Sidecar validated on port %d "
            "(connection deferred to first tool call)",
            port,
        )
        poll = 30.0


_MCP_INSTRUCTIONS = (
    "Ivy Language Server MCP tools for formal verification. "
    "Tools: ivy_verify (check), ivy_compile (ivyc), ivy_model_info (show), "
    "ivy_diagnostics (mode: structural|full), "
    "ivy_status (mode: capabilities|health), "
    "ivy_analysis (mode: includes|scope); "
    "ivy_coverage (mode: matrix|stats|gaps|diff), "
    "ivy_extract_requirements (output: structured|manifest); "
    "ivy_visualize (view: dependencies|state_machine|layers), "
    "ivy_model_summary (detail: summary|requirements); "
    "ivy_patterns (mode: analyze|validate|compare|check), "
    "ivy_pattern_scaffold; "
    "ivy_quality (mode: suggestions|gate); "
    "ivy_rfc_get (format: full|metadata|sections), "
    "ivy_rfc_search (keyword search via IETF Datatracker), "
    "ivy_rfc_section (section text + normative MUST/SHOULD/MAY analysis)."
)


# ---------------------------------------------------------------------------
# McpServerState — replaces the nonlocal closures formerly in start_mcp()
# ---------------------------------------------------------------------------


class McpServerState:
    """Mutable state for the MCP server.

    Replaces the 15+ nonlocal closures in ``start_mcp()`` with a proper
    class whose methods reference ``self.*`` instead of closure-captured
    ``nonlocal`` variables.
    """

    def __init__(
        self,
        root: str,
        staging_dir: str | None = None,
        semantic_model: Any = None,
        requirement_graph: Any = None,
        resolver: Any = None,
        include_paths: list[str] | None = None,
        exclude_dirs: frozenset[str] = frozenset(),
        executor: Any = None,
        base_path: str | None = None,
    ):
        """Initialise server state from the same arguments ``start_mcp()`` receives."""
        # Workspace root
        self.root = root
        self.staging_dir = staging_dir
        self.base_path = base_path

        # Config-driven timeouts/cooldowns
        _cfg = get_config()
        self._MODEL_RETRY_COOLDOWN = _cfg.model_retry_cooldown
        self._MODEL_BUILD_TIMEOUT = _cfg.model_build_timeout
        self._REQ_GRAPH_COOLDOWN = _cfg.req_graph_cooldown

        # Lazy SemanticModel (via LazyAsyncBuilder)
        self._model_builder: LazyAsyncBuilder[Any] = LazyAsyncBuilder(
            lambda: asyncio.to_thread(self._build_model),
            timeout=self._MODEL_BUILD_TIMEOUT,
            retry_cooldown=self._MODEL_RETRY_COOLDOWN,
            name="semantic_model",
        )
        if semantic_model is not None:
            self._model_builder.value = semantic_model

        # Lazy RequirementGraph (via LazyAsyncBuilder)
        self._graph_builder: LazyAsyncBuilder[Any] = LazyAsyncBuilder(
            lambda: asyncio.to_thread(self._build_requirement_graph),
            retry_cooldown=self._REQ_GRAPH_COOLDOWN,
            permanent_failure_check=lambda exc: isinstance(exc, ImportError),
            name="requirement_graph",
        )
        if requirement_graph is not None:
            self._graph_builder.value = requirement_graph

        # File caches
        self._cached_ivy_files: list[str] | None = None
        self._cached_ivy_files_lock = threading.Lock()
        self._basename_cache_obj = BasenameCache(self.find_ivy_files_cached, root)

        # Dedicated thread pool for MCP tool handlers — isolates tool
        # execution from the default pool used by model/graph builders,
        # preventing starvation during heavy background compilation.
        try:
            _pool_size = int(
                os.environ.get(
                    "IVY_LSP_TOOL_POOL_SIZE",
                    os.environ.get("IVY_LSP_MAX_CONCURRENT_TOOLS", "4"),
                )
            )
        except (ValueError, TypeError):
            _pool_size = 4
        self._tool_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_pool_size,
            thread_name_prefix="ivy-tool",
        )

        # Include resolution
        self._resolver = resolver
        self._include_paths = include_paths or []
        self._effective_exclude_dirs = DEFAULT_EXCLUDE_DIRS | exclude_dirs

        # Executor (Docker-aware compilation)
        self.executor = executor

        # Stdlib modules — populated after construction via discover_stdlib_modules()
        self.discovered_stdlib: frozenset[str] = frozenset()

        # Workspace context — set after build_tool_context() by start_mcp()
        self.workspace_context: Any = None

    # --- File finding ---

    def find_ivy_files(
        self, search_root: str, extra_paths: list[str] | None = None
    ) -> list[str]:
        """Find .ivy files respecting workspace include/exclude paths.

        When include_paths is set, walks only those subdirectories instead
        of scanning the entire workspace tree and post-filtering.

        Args:
            search_root: Root directory to search from.
            extra_paths: Additional relative paths to include in the scan
                regardless of workspace include_paths settings.
        """
        if self._include_paths or extra_paths:
            all_paths = list(self._include_paths) if self._include_paths else []
            if extra_paths:
                all_paths.extend(p for p in extra_paths if p not in all_paths)
            if all_paths:
                results: list[str] = []
                for ip in all_paths:
                    sub = os.path.join(search_root, ip)
                    if os.path.isdir(sub):
                        for rel in _find_ivy_files_raw(
                            sub, self._effective_exclude_dirs
                        ):
                            results.append(os.path.join(ip, rel))
                if not results and self._include_paths:
                    logger.warning(
                        "include_paths %s yielded no .ivy files under %s",
                        self._include_paths,
                        search_root,
                    )
                return sorted(set(results))
        return _find_ivy_files_raw(search_root, self._effective_exclude_dirs)

    def find_ivy_files_cached(self, search_root: str) -> list[str]:
        """Return cached file list, building on first call."""
        if self._cached_ivy_files is not None:
            return self._cached_ivy_files
        with self._cached_ivy_files_lock:
            if self._cached_ivy_files is not None:
                return self._cached_ivy_files
            self._cached_ivy_files = self.find_ivy_files(search_root)
            return self._cached_ivy_files

    # --- Basename cache ---

    def get_basename_cache(self) -> dict[str, list[str]]:
        """Build (or return cached) basename->[relative_paths] map for all .ivy files."""
        return self._basename_cache_obj.get()

    def make_resolve_callback(self):
        """Create a resolve callback for structural lint include checking.

        The basename cache is populated lazily on first call to avoid a
        full workspace scan when no include actually needs resolution
        (the common case for structural-only diagnostics).
        """
        _cache: dict[str, list[str]] | None = None

        def _resolve(inc_name: str, from_file: str) -> str | None:
            nonlocal _cache
            # Accept known stdlib modules
            if inc_name in self.discovered_stdlib:
                return f"<stdlib>/{inc_name}.ivy"
            # Try fast parent-directory resolution first (no cache needed)
            parent_dir = os.path.dirname(from_file)
            candidate = os.path.join(parent_dir, inc_name + ".ivy")
            if os.path.isfile(candidate):
                return candidate
            # Fall back to full workspace basename cache (lazy build)
            if _cache is None:
                _cache = self.get_basename_cache()
            candidates = _cache.get(inc_name)
            if candidates:
                return candidates[0]  # first match is sufficient for lint
            return None

        return _resolve

    # --- Lazy SemanticModel (delegated to LazyAsyncBuilder) ---

    @property
    def semantic_model(self) -> Any:
        """Return the cached SemanticModel, or ``None`` if not yet built."""
        return self._model_builder.value

    @semantic_model.setter
    def semantic_model(self, value: Any) -> None:
        """Set the cached SemanticModel directly (e.g. from constructor args)."""
        self._model_builder.value = value

    async def get_model(self) -> Any:
        """Return the semantic model, building one if needed."""
        return await self._model_builder.get()

    def get_model_status(self) -> dict:
        """Return the current model build status for error surfacing."""
        return self._model_builder.get_status()

    async def get_model_or_none(self, timeout: float = 5.0) -> Any:
        """Return the model if ready, or wait briefly if building. Never blocks long."""
        return await self._model_builder.get_or_wait(timeout)

    def invalidate_caches(self) -> None:
        """Reset all cached models and file lists.

        Called after ivy_index rebuilds to force fresh model construction
        from the updated .ivy-index/ on the next tool access.
        """
        self._model_builder.invalidate()
        self._graph_builder.invalidate()
        self._cached_ivy_files = None
        if self._basename_cache_obj is not None:
            self._basename_cache_obj.invalidate()

    def _write_model_to_index(self, model):
        write_model_to_index(
            root=self.root,
            model=model,
            workspace_context=self.workspace_context,
            find_ivy_files_fn=self.find_ivy_files,
        )

    def _build_model(self):
        return build_mcp_model(
            workspace_context=self.workspace_context,
            root=self.root,
            include_paths=self._include_paths,
            exclude_dirs=self._effective_exclude_dirs,
            resolver=self._resolver,
            find_ivy_files_fn=self.find_ivy_files_cached,
            resolve_callback=self.make_resolve_callback(),
            stdlib_modules=self.discovered_stdlib,
            write_model_fn=self._write_model_to_index,
        )

    # --- Lazy RequirementGraph (delegated to LazyAsyncBuilder) ---

    async def get_req_graph(self) -> Any:
        """Return the requirement graph, lazily building one if needed."""
        return await self._graph_builder.get()

    def _populate_semantic_model_from_graph(self, graph: Any) -> None:
        """Mirror RequirementGraph nodes and edges into the SemanticModel.

        The RequirementGraph bridges domain-specific extraction data into the
        unified SemanticModel so that both models stay consistent.  This
        function only *adds* to the SemanticModel, never replaces it.

        If the SemanticModel has not been built yet, this is a no-op.
        """
        if self.semantic_model is None:
            logger.debug(
                "SemanticModel not yet built; skipping requirement "
                "graph bridge (data will be available via "
                "RequirementGraph only)"
            )
            return

        try:
            from ivy_lsp.core.analysis.requirement_graph import EdgeType
            from ivy_lsp.core.semantic.edges import SemanticEdgeType

            # Map RequirementGraph EdgeType -> SemanticEdgeType
            _edge_type_map = {
                EdgeType.READS: SemanticEdgeType.READS,
                EdgeType.WRITES: SemanticEdgeType.WRITES,
                EdgeType.CONSTRAINS: SemanticEdgeType.CONSTRAINS,
                EdgeType.DEPENDS_ON: SemanticEdgeType.DEPENDS_ON,
                EdgeType.PROPAGATED_FROM: SemanticEdgeType.PROPAGATED_FROM,
                EdgeType.COVERS: SemanticEdgeType.COVERS,
            }

            model = self.semantic_model

            # Add nodes: requirements, actions, state vars, properties
            for req in graph.requirements.values():
                model.add_node(req)
            for action in graph.actions.values():
                model.add_node(action)
            for sv in graph.state_vars.values():
                model.add_node(sv)
            for prop in graph.properties.values():
                model.add_node(prop)

            # Add edges, translating EdgeType -> SemanticEdgeType
            for src, etype, dst in graph.edges:
                sem_etype = _edge_type_map.get(etype)
                if sem_etype is not None:
                    model.add_edge(src, sem_etype, dst)
                else:
                    logger.debug(
                        "Unmapped edge type %s in requirement graph; skipping",
                        etype,
                    )

            logger.info(
                "Bridged requirement graph into SemanticModel: "
                "%d requirements, %d actions, %d state vars, %d edges",
                len(graph.requirements),
                len(graph.actions),
                len(graph.state_vars),
                len(graph.edges),
            )
        except Exception:
            logger.warning(
                "Failed to bridge requirement graph into SemanticModel",
                exc_info=True,
            )

    def _build_requirement_graph(self):
        return build_requirement_graph(
            root=self.root,
            ivy_files=self.find_ivy_files_cached(self.root),
            resolver=self._resolver,
            include_paths=self._include_paths,
            exclude_dirs=self._effective_exclude_dirs,
            enrichment_adapter=getattr(self, "_enrichment", None),
            workspace_context=self.workspace_context,
            find_ivy_files_cached_fn=self.find_ivy_files_cached,
            populate_semantic_model_fn=self._populate_semantic_model_from_graph,
        )

    # --- Visualization server proxy ---

    async def make_viz_server_proxy(self):
        """Create a minimal server-like object for visualization handlers.

        The visualization handlers in features/visualization.py expect a
        server object with ``server.indexer.requirement_graph``.  This
        lazily builds the requirement graph on first use and returns a
        lightweight proxy that satisfies that contract.

        Uses ``get_or_wait`` to handle the case where another coroutine
        is already building the graph (``get`` would return None
        immediately in that case).
        """
        graph = await self._graph_builder.get_or_wait(timeout=15.0)
        if graph is None:
            graph = await self.get_req_graph()
        return _ServerProxy(
            indexer=_IndexerProxy(requirement_graph=graph),
            workspace_root=self.root,
        )

    # --- Pre-warming ---

    def prewarm(self, tool_context: ToolContext | None = None) -> None:
        """Load workspace context and pre-warm model/graph in background thread.

        The workspace context load is always deferred here (rather than running
        synchronously in start_mcp) so that the MCP stdio transport can begin
        accepting messages immediately.  Claude Code's MCP connection timeout
        is 30 s, but indexing 5+ protocols can take 50+ s.

        Args:
            tool_context: If provided, workspace_context will be set on both
                this McpServerState and the ToolContext once loading completes.
        """
        _cfg = get_config()
        _prewarm_model = _cfg.prewarm_model
        _prewarm_graph = os.environ.get("IVY_LSP_PREWARM_GRAPH", "1") != "0"

        def _prewarm_fn():
            import asyncio as _asyncio

            # --- Phase 1: load workspace context (must run before model/graph) ---
            try:
                from ivy_lsp.core.workspace.context import WorkspaceContext

                ws_ctx = WorkspaceContext.load(self.root)
                self.workspace_context = ws_ctx
                if tool_context is not None:
                    tool_context.workspace_context = ws_ctx
                if ws_ctx.has_index():
                    logger.info(
                        "[MCP-INDEX] Loaded offline index for: %s",
                        ", ".join(ws_ctx.list_protocols()),
                    )
                else:
                    logger.info("[MCP-INDEX] No offline indexes found")
            except Exception:
                logger.warning(
                    "[MCP-INDEX] WorkspaceContext loading failed",
                    exc_info=True,
                )

            # --- Phase 2: pre-warm model and graph ---
            if not (_prewarm_model or _prewarm_graph):
                logger.info("[MCP-PREWARM-DONE] Workspace loaded (prewarm disabled)")
                return

            loop = _asyncio.new_event_loop()
            try:
                if _prewarm_model:
                    logger.info("[INDEX-PREWARM] Starting model pre-warm...")
                    model = loop.run_until_complete(self.get_model())
                    if model is not None:
                        logger.info("[INDEX-MODEL-READY] SemanticModel pre-warmed")
                    else:
                        logger.warning(
                            "[INDEX-MODEL-READY] Model pre-warm returned None"
                        )
                if _prewarm_graph:
                    logger.info("[INDEX-PREWARM] Starting graph pre-warm...")
                    graph = loop.run_until_complete(self.get_req_graph())
                    if graph is not None:
                        logger.info("[INDEX-GRAPH-READY] RequirementGraph pre-warmed")
                    else:
                        logger.warning(
                            "[INDEX-GRAPH-READY] Graph pre-warm returned None"
                        )
            except Exception:
                logger.error("[INDEX-PREWARM] Pre-warming failed", exc_info=True)
            else:
                logger.info("[MCP-PREWARM-DONE] Background pre-warming complete")
            finally:
                loop.close()

        _prewarm_thread = threading.Thread(
            target=_prewarm_fn, name="mcp-prewarm", daemon=True
        )
        _prewarm_thread.start()
        logger.info("[INDEX-PREWARM] Background workspace load + pre-warming started")

    # --- ToolContext construction ---

    def build_tool_context(self) -> ToolContext:
        """Build a ToolContext wired to this state's methods."""
        from ivy_lsp.core.indexer.include_resolver import discover_stdlib_modules

        self.discovered_stdlib = discover_stdlib_modules()

        ctx = ToolContext(
            root=self.root,
            staging_dir=self.staging_dir,
            executor=self.executor,
            base_path=self.base_path,
            stdlib_modules=self.discovered_stdlib,
        )
        # Wire up callables backed by this state's methods
        ctx.find_ivy_files = self.find_ivy_files_cached
        ctx.get_model = self.get_model
        ctx.get_model_or_none = self.get_model_or_none
        ctx.get_model_status = self.get_model_status
        ctx.get_req_graph = self.get_req_graph
        ctx.make_viz_server_proxy = self.make_viz_server_proxy
        ctx.get_basename_cache = self.get_basename_cache
        ctx.make_resolve_callback = self.make_resolve_callback
        ctx.include_resolver = self._resolver
        ctx.tool_executor = self._tool_executor
        ctx.invalidate_caches = self.invalidate_caches

        # Wire up RFC service
        from ivy_lsp.core.rfc.service import RfcService
        from ivy_lsp.infra.config import get_config

        cfg = get_config()
        cache_dir = cfg.rfc_cache_dir
        if cache_dir is None and self.root:
            cache_dir = os.path.join(self.root, ".ivy-cache", "rfc")
        ctx.rfc_service = RfcService(
            cache_dir=cache_dir,
            cache_ttl=cfg.rfc_cache_ttl,
            local_dir=cfg.rfc_local_dir,
            offline=cfg.rfc_offline,
        )

        return ctx


def create_mcp_app(ctx: ToolContext) -> Any:
    """Create a FastMCP application with all Ivy tools registered.

    Pure tool registration — no startup logic. Used by both the standalone
    MCP stdio mode and the HTTP sidecar mode.

    Args:
        ctx: Shared ToolContext with workspace state and helpers.

    Returns:
        A configured FastMCP instance.
    """
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

    mcp = FastMCP("ivy-lsp", instructions=_MCP_INSTRUCTIONS)

    from ivy_lsp.mcp.tools import register_all_tools

    register_all_tools(mcp, ctx)
    return mcp
