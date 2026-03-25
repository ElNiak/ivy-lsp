"""MCP server mode for ivy-lsp.

Exposes Ivy verification tools, structured diagnostics, include graph,
and fast lint as MCP tools via the Model Context Protocol. Shares the
same parsing and indexing code as the LSP server.

Usage:
    python -m ivy_lsp --mcp
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ivy_lsp.core.verification import run_ivy_check as shared_ivy_check  # noqa: F401
from ivy_lsp.infra.config import get_config
from ivy_lsp.infra.observability import LogCategory, log_phase, timed_phase

# Re-export verification functions so that external code and tests that patch
# ``ivy_lsp.mcp_server.shared_ivy_check`` (etc.) continue to work after the
# tool handlers were moved to ``ivy_lsp.tools.*``.
from ivy_lsp.mcp import client as sidecar_client

logger = logging.getLogger(__name__)

# Note: Symbol/type extraction regex patterns formerly defined here have been
# relocated to ivy_lsp.parsing.tiered_extractor (Tier 3 fallback).  The
# _build_model() function now uses TieredExtractor for parser -> lexer -> regex
# cascade.  Include extraction is also handled by TieredExtractor.


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


from ivy_lsp.infra.utils.ivy_output import DEFAULT_EXCLUDE_DIRS
from ivy_lsp.infra.utils.ivy_output import find_ivy_files as _find_ivy_files_raw
from ivy_lsp.infra.utils.structural_lint import (
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
                return resolver.find_all_ivy_files()
            return _find_ivy_raw(search_root, _exclude)

        # Basename cache
        _basename_cache: dict[str, list[str]] | None = None
        _cache_lock = __import__("threading").Lock()

        def _get_basename_cache() -> dict[str, list[str]]:
            nonlocal _basename_cache
            if _basename_cache is not None:
                return _basename_cache
            with _cache_lock:
                if _basename_cache is not None:
                    return _basename_cache
                cache: dict[str, list[str]] = {}
                import os as _os

                for rel_path in _find_files(ws_root):
                    basename = _os.path.basename(rel_path)[:-4]
                    cache.setdefault(basename, []).append(rel_path)
                _basename_cache = cache
                return cache

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
            return {"state": "not_built"}

        ctx.get_model = _get_model
        ctx.get_model_status = _get_model_status
        ctx.get_req_graph = _get_req_graph
        ctx.get_basename_cache = _get_basename_cache

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

        # Visualization server proxy
        from dataclasses import dataclass as _dc

        @_dc
        class _IndexerProxy:
            requirement_graph: Any

        @_dc
        class _ServerProxy:
            indexer: _IndexerProxy
            initializing: bool = False
            workspace_root: str = ""

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

    ws_hash = sidecar_client.workspace_hash(workspace_root)
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

        # Already upgraded — just keep a slow heartbeat
        if sidecar_client.get_sidecar_client() is not None:
            poll = 30.0
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
    "ivy_diagnostics (mode: structural|full), ivy_include_graph, "
    "ivy_capabilities; "
    "ivy_coverage (mode: matrix|stats|gaps|diff), "
    "ivy_extract_requirements (output: structured|manifest); "
    "ivy_visualize (view: dependencies|state_machine|layers), "
    "ivy_model_summary (detail: summary|requirements); "
    "ivy_patterns (mode: analyze|validate|compare|check), "
    "ivy_pattern_scaffold; "
    "ivy_quality (mode: suggestions|gate)."
)


def create_mcp_app(ctx: ToolContext) -> Any:
    """Create a FastMCP application with all Ivy tools registered.

    Pure tool registration — no startup logic. Used by both the standalone
    MCP stdio mode and the HTTP sidecar mode.

    Args:
        ctx: Shared ToolContext with workspace state and helpers.

    Returns:
        A configured FastMCP instance.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("ivy-lsp", instructions=_MCP_INSTRUCTIONS)

    from ivy_lsp.mcp.tools import register_all_tools

    register_all_tools(mcp, ctx)
    return mcp


def start_mcp(
    workspace_root: str | None = None,
    semantic_model: Any = None,
    requirement_graph: Any = None,
    docker_image: str | None = None,
    base_path: str | None = None,
    staging_dir: str | None = None,
    _return_app: bool = False,
    ws_config: Any = None,
) -> Any:
    """Start the MCP server exposing Ivy tools.

    Args:
        workspace_root: Root directory for the workspace.
        semantic_model: Optional SemanticModel for shared-process mode.
        requirement_graph: Optional RequirementGraph (or ScopedRequirementModel)
            for visualization tools. When provided, enables ivy_model_summary
            (detail="requirements"), ivy_model_summary, and ivy_coverage (mode="gaps")
            MCP tools.
        docker_image: Docker image for Ivy compilation (e.g. "panther_ivy:latest").
            When set, compilation tools use Docker instead of native subprocess.
        base_path: Base protocol-testing path for compile command generation.
        staging_dir: Optional staging directory for include resolution.
            When set, ivy_verify/ivy_compile/ivy_model_info resolve paths
            through this directory (flat symlinks for CWD-relative includes).
        _return_app: Internal flag for testing. When True, returns the FastMCP
            instance without starting the server.
        ws_config: Optional workspace configuration object.
    """
    with timed_phase(
        logger,
        category=LogCategory.MILESTONE,
        phase="mcp",
        name="start_mcp",
        channel="mcp",
        payload={"workspace_root": workspace_root, "docker_image": docker_image},
    ):
        try:
            import mcp.server.fastmcp  # noqa: F401 — validate dependency
        except ImportError:
            raise ImportError(
                "MCP mode requires the 'mcp' package. "
                "Install with: pip install ivy-lsp[mcp]"
            )

        root = workspace_root or os.getcwd()
        log_phase(
            logger,
            category=LogCategory.MILESTONE,
            phase="mcp",
            message="MCP startup initialized",
            data={"root": root, "return_app": _return_app},
            level=logging.INFO,
        )

    # --- Workspace scoping: respect include/exclude paths from detection ---
    if ws_config is not None:
        _include_paths = ws_config.include_paths
        _extra_exclude_dirs = frozenset(ws_config.exclude_paths)
    else:
        _cfg = get_config()
        _include_paths = _cfg.include_paths
        _extra_exclude_dirs = frozenset(_cfg.exclude_paths)
    _effective_exclude_dirs = DEFAULT_EXCLUDE_DIRS | _extra_exclude_dirs

    def _find_ivy_files(search_root: str) -> list[str]:
        """Find .ivy files respecting workspace include/exclude paths.

        When include_paths is set, walks only those subdirectories instead
        of scanning the entire workspace tree and post-filtering.
        """
        if _include_paths:
            results: list[str] = []
            for ip in _include_paths:
                sub = os.path.join(search_root, ip)
                if os.path.isdir(sub):
                    for rel in _find_ivy_files_raw(sub, _effective_exclude_dirs):
                        results.append(os.path.join(ip, rel))
            return sorted(set(results))
        return _find_ivy_files_raw(search_root, _effective_exclude_dirs)

    # --- Cached file list (avoids redundant directory traversals) ---
    _cached_ivy_files: list[str] | None = None
    _cached_ivy_files_lock = threading.Lock()

    def _find_ivy_files_cached(search_root: str) -> list[str]:
        """Return cached file list, building on first call."""
        nonlocal _cached_ivy_files
        if _cached_ivy_files is not None:
            return _cached_ivy_files
        with _cached_ivy_files_lock:
            if _cached_ivy_files is not None:
                return _cached_ivy_files
            _cached_ivy_files = _find_ivy_files(search_root)
            return _cached_ivy_files

    if _include_paths:
        logger.info("Workspace include paths: %s", _include_paths)
    if _extra_exclude_dirs:
        logger.info("Workspace extra exclude dirs: %s", _extra_exclude_dirs)

    # --- Include resolver for cross-directory includes ---
    _resolver = None
    try:
        from ivy_lsp.core.indexer.include_resolver import IncludeResolver

        # Use passed-in ws_config; only re-detect if not provided
        if ws_config is None:
            from ivy_lsp.core.workspace.detection import detect_ivy_workspace

            ws_config = detect_ivy_workspace(start_dir=root)
            logger.info(
                "MCP fallback workspace detection: root=%s, detected_by=%s",
                ws_config.workspace_root,
                ws_config.detected_by,
            )

        _stdlib_path = None
        if ws_config.standard_library:
            _stdlib_path = os.path.join(root, ws_config.standard_library)

        _resolver = IncludeResolver(
            root,
            ivy_include_path=_stdlib_path,
            exclude_paths=list(_extra_exclude_dirs),
            include_paths=_include_paths or [],
            workspace_layers=ws_config.workspace_layers,
        )
        _staging = _resolver.create_staging_directory()
        logger.info("MCP staging directory: %s", _staging)
        if ws_config.workspace_layers:
            _resolver.build_layered_staging()
            logger.info(
                "Built layered staging for %d layers",
                len(ws_config.workspace_layers),
            )
        # Use resolver's staging dir if none was explicitly passed
        if _resolver._staging_dir and not staging_dir:
            staging_dir = _resolver._staging_dir
    except Exception:
        logger.warning(
            "IncludeResolver init failed; tier-1 parsing will be limited",
            exc_info=True,
        )

    # Create executor for Docker-aware compilation
    executor = None
    if docker_image:
        try:
            from panther_ivy.api.executor import IvyExecutor

            executor = IvyExecutor(docker_image=docker_image)
            logger.info("Docker executor configured with image: %s", docker_image)
        except ImportError:
            logger.warning(
                "panther_ivy.api.executor not available; "
                "falling back to native subprocess"
            )
    _model_lock = asyncio.Lock()
    _model_build_attempted: float = 0.0  # timestamp of last failed attempt
    _model_build_error: str | None = None
    _model_building: bool = False  # True while a build is in progress
    _cfg = get_config()
    _MODEL_RETRY_COOLDOWN = _cfg.model_retry_cooldown
    _MODEL_BUILD_TIMEOUT = _cfg.model_build_timeout

    _req_graph_lock = asyncio.Lock()
    _req_graph: Any = requirement_graph  # may be pre-populated or None
    _req_graph_import_failed = False  # permanent flag for ImportError
    _req_graph_last_failure: float = 0.0  # timestamp of last non-import failure
    _REQ_GRAPH_COOLDOWN = _cfg.req_graph_cooldown

    # --- Workspace-aware include resolution cache ---

    _basename_cache: dict[str, list[str]] | None = None
    _basename_cache_lock = threading.Lock()

    def _get_basename_cache() -> dict[str, list[str]]:
        """Build (or return cached) basename->[relative_paths] map for all .ivy files."""
        nonlocal _basename_cache
        if _basename_cache is not None:
            return _basename_cache

        with _basename_cache_lock:
            if _basename_cache is not None:
                return _basename_cache
            cache: dict[str, list[str]] = {}
            for rel_path in _find_ivy_files_cached(root):
                basename = os.path.basename(rel_path)[:-4]  # strip .ivy
                cache.setdefault(basename, []).append(rel_path)
            _basename_cache = cache
            return cache

    def _make_resolve_callback():
        """Create a resolve callback for structural lint include checking."""
        cache = _get_basename_cache()

        def _resolve(inc_name: str, from_file: str) -> str | None:
            # Accept known stdlib modules
            if inc_name in ctx.stdlib_modules:
                return f"<stdlib>/{inc_name}.ivy"
            # Check workspace file index
            candidates = cache.get(inc_name)
            if candidates:
                return candidates[0]  # first match is sufficient for lint
            return None

        return _resolve

    # --- Lazy SemanticModel construction ---

    async def _get_model():
        """Return the semantic model, building one if needed."""
        nonlocal semantic_model, _model_build_attempted, _model_build_error, _model_building
        # Fast path: model already built
        if semantic_model is not None:
            return semantic_model
        # Fast path: previous build failed and cooldown has not elapsed
        if (
            _model_build_attempted
            and (time.monotonic() - _model_build_attempted) < _MODEL_RETRY_COOLDOWN
        ):
            return None
        # Early return if another coroutine is already building the model
        if _model_building:
            return None

        async with _model_lock:
            # Double-check after acquiring lock
            if semantic_model is not None:
                return semantic_model
            if (
                _model_build_attempted
                and (time.monotonic() - _model_build_attempted) < _MODEL_RETRY_COOLDOWN
            ):
                return None

            _model_building = True
            try:
                model = await asyncio.wait_for(
                    asyncio.to_thread(_build_model),
                    timeout=_MODEL_BUILD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("Model build timed out after %.0fs", _MODEL_BUILD_TIMEOUT)
                _model_build_attempted = time.monotonic()
                _model_build_error = (
                    f"Build timed out after {_MODEL_BUILD_TIMEOUT:.0f}s"
                )
                return None
            except Exception as exc:
                logger.error("Model build failed: %s", exc, exc_info=True)
                _model_build_attempted = time.monotonic()
                _model_build_error = str(exc)
                return None
            finally:
                _model_building = False
            if model is not None:
                semantic_model = model
            else:
                _model_build_attempted = time.monotonic()
                _model_build_error = (
                    "Build returned empty model (missing dependencies?)"
                )
            return model

    def _get_model_status() -> dict:
        """Return the current model build status for error surfacing."""
        if semantic_model is not None:
            return {"state": "ready"}
        if _model_building:
            return {"state": "building"}
        if _model_build_error:
            elapsed = time.monotonic() - _model_build_attempted
            remaining = max(0, _MODEL_RETRY_COOLDOWN - elapsed)
            return {
                "state": "failed",
                "error": _model_build_error,
                "retry_in_seconds": round(remaining),
            }
        return {"state": "not_built"}

    async def _get_model_or_none(timeout: float = 5.0):
        """Return the model if ready, or wait briefly if building. Never blocks long.

        Unlike _get_model(), this will NOT trigger a full build. If the model
        hasn't started building yet, it kicks off a background build and returns
        None immediately. If it's currently building, waits up to *timeout*
        seconds for it to finish.
        """
        if semantic_model is not None:
            return semantic_model
        if not _model_building:
            # Kick off background build, but don't wait for it
            asyncio.ensure_future(_get_model())
            return None
        # Model is currently building — wait briefly
        deadline = time.monotonic() + timeout
        while _model_building and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        return semantic_model  # may still be None

    def _write_model_to_index(model):
        """Write a SemanticModel to .ivy-index/ per-protocol directories.

        Uses fcntl file locking (same pattern as IndexBuilder._write_pickle()).
        Creates output files alongside existing index artifacts so subsequent
        MCP startups can load them via Strategy 1.
        """
        try:
            import fcntl
            import gzip
            import pickle

            if ctx.workspace_context is None:
                return

            # Write to each protocol's .ivy-index/ directory
            for proto, idx in ctx.workspace_context.protocol_indexes.items():
                index_dir = idx.index_dir
                if not index_dir or not os.path.isdir(index_dir):
                    continue
                lock_path = os.path.join(index_dir, ".build.lock")
                try:
                    lock_fd = open(lock_path, "w")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        out_path = os.path.join(index_dir, "semantic_model.pickle.gz")
                        with gzip.open(out_path, "wb") as f:
                            pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)
                        logger.info("Wrote model to %s", out_path)
                    except BlockingIOError:
                        logger.debug("Index lock held for %s, skipping write", proto)
                    finally:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                        lock_fd.close()
                except OSError:
                    logger.debug("Cannot write to %s index dir", proto, exc_info=True)

            # Also try workspace-level .ivy-index/ if it exists
            ws_index = os.path.join(root, ".ivy-index")
            if os.path.isdir(ws_index):
                lock_path = os.path.join(ws_index, ".build.lock")
                try:
                    lock_fd = open(lock_path, "w")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        out_path = os.path.join(ws_index, "semantic_model.pickle.gz")
                        with gzip.open(out_path, "wb") as f:
                            pickle.dump(model, f, protocol=pickle.HIGHEST_PROTOCOL)
                        logger.info("Wrote model to %s", out_path)
                    except BlockingIOError:
                        pass
                    finally:
                        try:
                            fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        except Exception:
                            pass
                        lock_fd.close()
                except OSError:
                    pass
        except Exception:
            logger.debug("Failed to write model to .ivy-index/", exc_info=True)

    def _build_model():
        """Build a lightweight semantic model from workspace files.

        Tries two strategies in order:
        1. Offline index merge (per-protocol models from .ivy-index/)
        2. Full rebuild via TieredExtractor

        After a successful build, writes the model back to .ivy-index/
        per-protocol directories so subsequent startups are instant.
        """
        nonlocal _req_graph

        # --- Strategy 1: Merge per-protocol models from offline index ---
        try:
            if ctx.workspace_context is not None and ctx.workspace_context.has_index():
                from ivy_lsp.core.semantic.model import SemanticModel

                merged = SemanticModel()
                used_protos: list[str] = []
                skipped_protos: list[str] = []
                for proto, idx in ctx.workspace_context.protocol_indexes.items():
                    if idx.semantic_model is None:
                        skipped_protos.append(f"{proto}(no model)")
                        continue
                    if idx.staleness.status not in ("fresh", "stale_minor"):
                        skipped_protos.append(f"{proto}({idx.staleness.status})")
                        continue
                    merged.merge_from(idx.semantic_model)
                    used_protos.append(proto)

                if used_protos and merged.node_count() > 0:
                    logger.info(
                        "Loaded semantic model from offline index: "
                        "%d nodes from %s (skipped: %s)",
                        merged.node_count(),
                        ", ".join(used_protos),
                        ", ".join(skipped_protos) or "none",
                    )
                    return merged
                elif skipped_protos:
                    logger.info(
                        "Offline index incomplete, falling back to full build "
                        "(skipped: %s)",
                        ", ".join(skipped_protos),
                    )
        except Exception:
            logger.debug(
                "Offline index merge failed, falling back to full build",
                exc_info=True,
            )

        # --- Strategy 2: Full rebuild from scratch ---
        from ivy_lsp.core.semantic.model_builder import build_semantic_model

        model = build_semantic_model(
            root=root,
            find_files_fn=_find_ivy_files_cached,
            include_resolver=(
                _resolver.resolve if _resolver else _make_resolve_callback()
            ),
            stdlib_modules=discovered_stdlib,
        )

        # Write rebuilt model to .ivy-index/ so next startup uses Strategy 1
        if model is not None:
            _write_model_to_index(model)

        return model

    # --- Lazy RequirementGraph construction ---

    async def _get_req_graph():
        """Return the requirement graph, lazily building one if needed."""
        nonlocal _req_graph, _req_graph_import_failed, _req_graph_last_failure
        if _req_graph is not None:
            return _req_graph
        # Permanent failure: missing dependency
        if _req_graph_import_failed:
            return None
        # Transient failure: respect cooldown
        if _req_graph_last_failure and (
            time.monotonic() - _req_graph_last_failure < _REQ_GRAPH_COOLDOWN
        ):
            return None

        async with _req_graph_lock:
            if _req_graph is not None:
                return _req_graph
            if _req_graph_import_failed:
                return None
            if _req_graph_last_failure and (
                time.monotonic() - _req_graph_last_failure < _REQ_GRAPH_COOLDOWN
            ):
                return None

            try:
                logger.info(
                    "Building requirement graph (first call, may take 1-2 min)..."
                )
                graph = await asyncio.to_thread(_build_requirement_graph)
            except ImportError as exc:
                logger.warning(
                    "Requirement graph unavailable (missing dependency): %s",
                    exc,
                )
                _req_graph_import_failed = True
                return None
            except Exception as exc:
                logger.warning(
                    "Requirement graph build failed (will retry in %ds): %s",
                    _REQ_GRAPH_COOLDOWN,
                    exc,
                )
                _req_graph_last_failure = time.monotonic()
                return None

            if graph is not None:
                _req_graph = graph
            else:
                _req_graph_last_failure = time.monotonic()
            return graph

    def _populate_semantic_model_from_graph(graph: Any) -> None:
        """Mirror RequirementGraph nodes and edges into the SemanticModel.

        This bridges the domain-specific RequirementGraph data into the
        unified SemanticModel so that both models stay consistent.
        The RequirementGraph is kept as a compatibility layer — this
        function only *adds* to the SemanticModel, never replaces it.

        If the SemanticModel has not been built yet, this is a no-op.
        """
        if semantic_model is None:
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

            model = semantic_model

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

    def _build_requirement_graph():
        """Build a RequirementGraph from workspace .ivy files.

        Tries the offline index first (per-protocol requirement graphs
        from .ivy-index/), then falls back to a full build using the
        light-mode extractor.
        """
        # Try offline index before doing the expensive build
        try:
            if ctx.workspace_context is not None and ctx.workspace_context.has_index():
                for _proto, idx in ctx.workspace_context.protocol_indexes.items():
                    if idx.requirement_graph is not None:
                        logger.info("Loaded requirement graph from offline index")
                        return idx.requirement_graph
        except Exception:
            logger.debug(
                "Offline index lookup failed for requirement graph",
                exc_info=True,
            )

        try:
            from ivy_lsp.core.analysis.light_mode_extractor import (
                extract_requirements_light,
            )
            from ivy_lsp.core.analysis.requirement_graph import (
                ActionNode,
                RequirementGraph,
                StateVarNode,
            )

            t0 = time.monotonic()
            graph = RequirementGraph()
            all_writes: list[tuple[str, str, int]] = []
            known_vars: set[str] = set()

            discovered = _find_ivy_files_cached(root)
            logger.info(
                "Requirement graph: discovered %d .ivy files (root=%s, include_paths=%s)",
                len(discovered),
                root,
                _include_paths or "(all)",
            )
            if not discovered:
                logger.warning(
                    "Requirement graph: no .ivy files found — graph will be empty. "
                    "Check workspace root and include_paths."
                )
                return None

            files_scanned = 0
            for rel_path in discovered:
                abs_path = os.path.join(root, rel_path)
                try:
                    with open(abs_path, encoding="utf-8", errors="replace") as f:
                        source = f.read()
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
                    continue

                files_scanned += 1
                reqs, writes = extract_requirements_light(source, abs_path)
                if not reqs and not writes:
                    continue

                # Bulk-add requirements + CONSTRAINS edges for this file
                graph.add_file_requirements(abs_path, reqs, writes)

                # Collect write targets as known state vars
                for var_name, _fp, _line in writes:
                    known_vars.add(var_name)
                all_writes.extend(writes)

            t1 = time.monotonic()
            logger.info(
                "Requirement graph: file indexing done — %d files in %.1fs",
                files_scanned,
                t1 - t0,
            )

            # Create ActionNodes from monitor_action references
            for req in graph.requirements.values():
                if req.monitor_action:
                    graph.add_action_if_absent(
                        ActionNode(
                            id=req.monitor_action,
                            name=req.monitor_action.rsplit(".", 1)[-1],
                            qualified_name=req.monitor_action,
                            file=req.file,
                            line=req.line,
                        )
                    )

            # Create StateVarNodes from write targets
            for var_name, filepath_w, line_w in all_writes:
                if var_name not in graph.state_vars:
                    graph.add_state_var(
                        StateVarNode(
                            id=var_name,
                            name=var_name.rsplit(".", 1)[-1],
                            qualified_name=var_name,
                            file=filepath_w,
                            line=line_w,
                        )
                    )

            # Wire READS edges from requirements to state vars
            if known_vars:
                try:
                    graph.wire_state_var_edges(known_vars)
                except ImportError:
                    logger.debug(
                        "formula_analyzer unavailable; skipping READS edge wiring"
                    )

            t2 = time.monotonic()
            logger.info(
                "Requirement graph: edge wiring done — %d vars in %.1fs",
                len(known_vars),
                t2 - t1,
            )

            # Load RFC requirement manifests and wire COVERS edges
            try:
                from ivy_lsp.core.semantic.rfc_annotations import (
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

            t3 = time.monotonic()
            total = len(graph.requirements) + len(graph.actions) + len(graph.state_vars)
            logger.info(
                "Built requirement graph in %.1fs: %d requirements, %d actions, "
                "%d state vars, %d edges "
                "(indexing=%.1fs, wiring=%.1fs, manifests=%.1fs)",
                t3 - t0,
                len(graph.requirements),
                len(graph.actions),
                len(graph.state_vars),
                len(graph.edges),
                t1 - t0,
                t2 - t1,
                t3 - t2,
            )

            # --- Populate SemanticModel with the same data (compatibility bridge) ---
            # The SemanticModel may already be built (via _get_model); if so, mirror
            # the RequirementGraph nodes and edges into it.  If the model hasn't been
            # built yet, we skip — the requirement graph remains the source of truth
            # until the model is constructed.
            _populate_semantic_model_from_graph(graph)

            if total == 0:
                logger.warning(
                    "Requirement graph built but empty: %d files scanned, "
                    "0 requirements/actions/vars extracted. "
                    "Files may lack monitors or RFC annotations.",
                    files_scanned,
                )
                return None
            return graph
        except ImportError as exc:
            logger.warning(
                "Requirement graph build failed (missing dependency): %s", exc
            )
            return None
        except Exception:
            logger.warning(
                "Failed to build requirement graph",
                exc_info=True,
            )
            return None

    # --- Visualization server proxy ---

    from dataclasses import dataclass as _dc

    @_dc
    class _IndexerProxy:
        requirement_graph: Any

    @_dc
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

    # --- Build ToolContext and register tools from sub-modules ---

    from ivy_lsp.core.indexer.include_resolver import discover_stdlib_modules

    discovered_stdlib = discover_stdlib_modules()

    ctx = ToolContext(
        root=root,
        staging_dir=staging_dir,
        executor=executor,
        base_path=base_path,
        stdlib_modules=discovered_stdlib,
    )
    # Wire up callables that close over start_mcp's local state
    ctx.find_ivy_files = _find_ivy_files_cached
    ctx.get_model = _get_model
    ctx.get_model_or_none = _get_model_or_none
    ctx.get_model_status = _get_model_status
    ctx.get_req_graph = _get_req_graph
    ctx.make_viz_server_proxy = _make_viz_server_proxy
    ctx.get_basename_cache = _get_basename_cache
    ctx.make_resolve_callback = _make_resolve_callback
    ctx.include_resolver = _resolver

    # Populate workspace_groups from ws_config (if available)
    if ws_config is not None and hasattr(ws_config, "workspace_groups"):
        ctx.workspace_groups = ws_config.workspace_groups or {}

    # Load workspace context from .ivy-index/ if available
    try:
        from ivy_lsp.core.workspace.context import WorkspaceContext

        ctx.workspace_context = WorkspaceContext.load(root)
        if ctx.workspace_context.has_index():
            logger.info(
                "MCP loaded offline index for: %s",
                ", ".join(ctx.workspace_context.list_protocols()),
            )
    except Exception:
        logger.debug("WorkspaceContext loading failed in MCP", exc_info=True)

    mcp = create_mcp_app(ctx)

    # Log MCP-READY immediately after tool registration — tools are callable now.
    # This is polled by wait-for-indexing.sh; logging it before prewarm avoids
    # a 5-15s delay in readiness detection.
    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    logger.info("[MCP-READY] Server initialized, tools registered")

    # --- Start or return ---

    if _return_app:
        return mcp

    # --- Background pre-warming (non-blocking) ---
    _prewarm_model = _cfg.prewarm_model
    _prewarm_graph = os.environ.get("IVY_LSP_PREWARM_GRAPH", "1") != "0"

    if _prewarm_model or _prewarm_graph:
        import threading as _threading

        def _prewarm():
            """Pre-warm model and graph in background thread."""
            import asyncio as _asyncio

            loop = _asyncio.new_event_loop()
            try:
                if _prewarm_model:
                    logger.info("[INDEX-PREWARM] Starting model pre-warm...")
                    model = loop.run_until_complete(_get_model())
                    if model is not None:
                        logger.info("[INDEX-MODEL-READY] SemanticModel pre-warmed")
                    else:
                        logger.warning(
                            "[INDEX-MODEL-READY] Model pre-warm returned None"
                        )
                if _prewarm_graph:
                    logger.info("[INDEX-PREWARM] Starting graph pre-warm...")
                    graph = loop.run_until_complete(_get_req_graph())
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

        _prewarm_thread = _threading.Thread(
            target=_prewarm, name="mcp-prewarm", daemon=True
        )
        _prewarm_thread.start()
        logger.info("[INDEX-PREWARM] Background pre-warming started")

    # Start sidecar upgrade monitor in a daemon thread
    # (FastMCP's run() blocks the main event loop, so we use a separate thread)
    if os.environ.get("IVY_MCP_DISABLE_UPGRADE") != "1":

        def _start_monitor_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_sidecar_monitor(root))
            finally:
                loop.close()

        monitor_thread = threading.Thread(
            target=_start_monitor_in_thread,
            name="sidecar-monitor",
            daemon=True,
        )
        monitor_thread.start()
        logger.info("[SIDECAR-MONITOR] Started background upgrade monitor")

    mcp.run(transport="stdio")
