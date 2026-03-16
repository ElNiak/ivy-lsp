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
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# Re-export verification functions so that external code and tests that patch
# ``ivy_lsp.mcp_server.shared_ivy_check`` (etc.) continue to work after the
# tool handlers were moved to ``ivy_lsp.tools.*``.
from ivy_lsp.verification import run_ivy_check as shared_ivy_check  # noqa: F401
from ivy_lsp.verification import run_ivy_compile as shared_ivy_compile
from ivy_lsp.verification import run_ivy_show as shared_ivy_show

logger = logging.getLogger(__name__)

# Pre-compiled regex patterns for hot-path performance
_INCLUDE_PATTERN = re.compile(r"^include\s+(\w+)", re.MULTILINE)

# Symbol/type extraction patterns for lightweight semantic model
_TYPE_DECL_RE = re.compile(r"^\s*type\s+([\w.]+)(?:\s*=\s*\{([^}]+)\})?", re.MULTILINE)
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
_INDIVIDUAL_DECL_RE = re.compile(r"^\s*individual\s+([\w.]+)\s*:\s*(\w+)", re.MULTILINE)
_OBJECT_DECL_RE = re.compile(
    r"^\s*(object|module|isolate)\s+([\w.]+)\s*(?:=\s*\{)?", re.MULTILINE
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


from ivy_lsp.utils.ivy_output import DEFAULT_EXCLUDE_DIRS
from ivy_lsp.utils.ivy_output import find_ivy_files as _find_ivy_files_raw
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
    get_model_status: Callable[..., dict] = field(
        default=lambda: {"state": "not_built"}
    )
    get_req_graph: Callable[..., Any] = field(default=lambda: None)
    make_viz_server_proxy: Callable[..., Any] = field(default=lambda: None)
    get_basename_cache: Callable[..., dict[str, list[str]]] = field(default=lambda: {})
    make_resolve_callback: Callable[..., Any] = field(default=lambda: None)

    # Known Ivy standard library modules
    stdlib_modules: frozenset[str] = frozenset(
        {
            "order",
            "collections",
            "ip",
            "ipv6",
            "tcp",
            "udp",
            "byte_stream",
            "timeout",
            "net",
        }
    )

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
        raise ImportError(
            "MCP mode requires the 'mcp' package. "
            "Install with: pip install ivy-lsp[mcp]"
        )

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
            f
            for f in all_files
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
            from panther_ivy.api.executor import IvyExecutor

            executor = IvyExecutor(docker_image=docker_image)
            logger.info("Docker executor configured with image: %s", docker_image)
        except ImportError:
            logger.warning(
                "panther_ivy.api.executor not available; "
                "falling back to native subprocess"
            )
    mcp = FastMCP(
        "ivy-lsp",
        instructions=(
            "Ivy Language Server MCP tools for formal verification. "
            "Tools: ivy_verify (check), ivy_compile (ivyc), ivy_model_info (show), "
            "ivy_lint (fast linting), ivy_diagnostics, ivy_include_graph, "
            "ivy_capabilities; "
            "ivy_coverage (mode: matrix|stats|gaps), "
            "ivy_query (mode: impact|xrefs|info), "
            "ivy_extract_requirements (output: structured|manifest); "
            "ivy_visualize (view: dependencies|state_machine|layers), "
            "ivy_model_summary (detail: summary|requirements); "
            "ivy_patterns (mode: analyze|validate|compare|check), "
            "ivy_pattern_scaffold; "
            "ivy_quality (mode: suggestions|gate)."
        ),
    )

    _model_lock = asyncio.Lock()
    _model_build_attempted: float = 0.0  # timestamp of last failed attempt
    _model_build_error: str | None = None
    _MODEL_RETRY_COOLDOWN = 30.0  # seconds before retry after failure

    _req_graph_lock = asyncio.Lock()
    _req_graph: Any = requirement_graph  # may be pre-populated or None
    _req_graph_import_failed = False  # permanent flag for ImportError
    _req_graph_last_failure: float = 0.0  # timestamp of last non-import failure
    _REQ_GRAPH_COOLDOWN = 30.0  # seconds before retry after transient failure

    # --- Workspace-aware include resolution cache ---

    # Known Ivy standard library modules that should never be flagged
    # as unresolved by lint (they live in ivy/include/ or ivy2/).
    _STDLIB_MODULES = frozenset(
        {
            "order",
            "collections",
            "ip",
            "ipv6",
            "tcp",
            "udp",
            "byte_stream",
            "timeout",
            "net",
        }
    )

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

    # --- Lazy SemanticModel construction ---

    async def _get_model():
        """Return the semantic model, building one if needed."""
        nonlocal semantic_model, _model_build_attempted, _model_build_error
        # Fast path: model already built
        if semantic_model is not None:
            return semantic_model
        # Fast path: previous build failed and cooldown has not elapsed
        if (
            _model_build_attempted
            and (time.monotonic() - _model_build_attempted) < _MODEL_RETRY_COOLDOWN
        ):
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

            try:
                model = await asyncio.to_thread(_build_model)
            except Exception as exc:
                logger.error("Model build failed: %s", exc, exc_info=True)
                _model_build_attempted = time.monotonic()
                _model_build_error = str(exc)
                return None
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
        if _model_build_error:
            elapsed = time.monotonic() - _model_build_attempted
            remaining = max(0, _MODEL_RETRY_COOLDOWN - elapsed)
            return {
                "state": "failed",
                "error": _model_build_error,
                "retry_in_seconds": round(remaining),
            }
        return {"state": "not_built"}

    def _build_model():
        """Build a lightweight semantic model from workspace files.

        Returns the model on success, or ``None`` when a required
        dependency is missing (logged at WARNING).  The caller
        (``_get_model``) is responsible for caching the result and
        assigning the ``semantic_model`` nonlocal under the lock.
        """
        # Import required modules — narrow ImportError to just these imports
        try:
            from ivy_lsp.semantic.model import SemanticModel
            from ivy_lsp.semantic.nodes import (
                RfcAnnotation,
                RfcRequirement,
                SymbolNode,
                TypeNode,
            )
            from ivy_lsp.semantic.rfc_annotations import (
                find_manifests,
                load_requirement_manifest,
                parse_file_rfc_annotations,
            )
        except ImportError:
            logger.warning(
                "Semantic model unavailable: required modules "
                "(ivy_lsp.semantic.model or ivy_lsp.semantic.rfc_annotations) "
                "could not be imported. Install ivy-lsp[semantic] to enable "
                "traceability tools.",
                exc_info=True,
            )
            return None

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
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
                continue

            # RFC annotations
            for ann in parse_file_rfc_annotations(source, abs_path):
                model.add_node(ann)

            # Type declarations
            for m in _TYPE_DECL_RE.finditer(source):
                name = m.group(1)
                line = source[: m.start()].count("\n") + 1
                variants_raw = m.group(2)
                is_enum = variants_raw is not None
                variants = (
                    [v.strip() for v in variants_raw.split(",") if v.strip()]
                    if variants_raw
                    else []
                )
                model.add_node(
                    TypeNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        file=abs_path,
                        line=line,
                        is_enum=is_enum,
                        variants=variants,
                    )
                )

            # Action declarations
            for m in _ACTION_DECL_RE.finditer(source):
                name = m.group(1)
                line = source[: m.start()].count("\n") + 1
                params = (
                    [p.strip() for p in m.group(2).split(",") if p.strip()]
                    if m.group(2)
                    else []
                )
                ret = m.group(3).strip() if m.group(3) else None
                model.add_node(
                    SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="action",
                        file=abs_path,
                        line=line,
                        params=params,
                        return_sort=ret,
                    )
                )

            # Relation declarations
            for m in _RELATION_DECL_RE.finditer(source):
                name = m.group(1)
                line = source[: m.start()].count("\n") + 1
                model.add_node(
                    SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="relation",
                        file=abs_path,
                        line=line,
                    )
                )

            # Function declarations
            for m in _FUNCTION_DECL_RE.finditer(source):
                name = m.group(1)
                line = source[: m.start()].count("\n") + 1
                ret_sort = m.group(3) if m.group(3) else None
                model.add_node(
                    SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="function",
                        file=abs_path,
                        line=line,
                        return_sort=ret_sort,
                    )
                )

            # Individual declarations
            for m in _INDIVIDUAL_DECL_RE.finditer(source):
                name = m.group(1)
                line = source[: m.start()].count("\n") + 1
                sort_name = m.group(2)
                model.add_node(
                    SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind="individual",
                        file=abs_path,
                        line=line,
                        sort_name=sort_name,
                    )
                )

            # Object/module/isolate declarations
            for m in _OBJECT_DECL_RE.finditer(source):
                keyword = m.group(1)  # "object", "module", or "isolate"
                name = m.group(2)
                line = source[: m.start()].count("\n") + 1
                model.add_node(
                    SymbolNode(
                        id=f"{abs_path}:{line}:{name}",
                        name=name,
                        qualified_name=name,
                        kind=keyword,  # type: ignore[arg-type]
                        file=abs_path,
                        line=line,
                    )
                )

        # -- Wire semantic edges --
        from ivy_lsp.semantic.edges import SemanticEdgeType

        # 1. COVERS: RfcAnnotation -> RfcRequirement
        # Use normalize_tag_to_manifest_ids for proper tag resolution:
        # bare numbers like "4" match "rfc9000:4.*", section refs like
        # "4.1" match "rfc9000:4.1", and qualified tags match exactly.
        from ivy_lsp.semantic.rfc_annotations import normalize_tag_to_manifest_ids

        req_by_id: dict[str, object] = {
            n.id: n for n in model.get_nodes_by_type(RfcRequirement)
        }
        req_id_set = set(req_by_id.keys())
        for ann in model.get_nodes_by_type(RfcAnnotation):
            for tag in ann.tags:
                matched_ids = normalize_tag_to_manifest_ids(tag, req_id_set)
                for req_id in matched_ids:
                    model.add_edge(ann.id, SemanticEdgeType.COVERS, req_id)

        # 2. HAS_PARAM / RETURNS_TYPE: SymbolNode -> TypeNode
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
                    target = type_by_name.get(base) or type_by_name.get(type_ref)
                    if target:
                        model.add_edge(sn.id, SemanticEdgeType.HAS_PARAM, target)

            # RETURNS_TYPE
            ret = getattr(sn, "return_sort", None)
            if ret:
                base = ret.split(".")[-1]
                target = type_by_name.get(base) or type_by_name.get(ret)
                if target:
                    model.add_edge(sn.id, SemanticEdgeType.RETURNS_TYPE, target)

        # 3. INCLUDES: file -> file (via include directives)
        # Build basename -> abs_path map for resolution
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
            except OSError as exc:
                logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
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
            from ivy_lsp.analysis.requirement_graph import EdgeType
            from ivy_lsp.semantic.edges import SemanticEdgeType

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

        Uses the light-mode extractor (regex or PLY lexer) to extract
        requirements and writes from each file, populates ActionNode
        and RequirementNode entries, wires CONSTRAINS and WRITES edges.
        """
        try:
            from ivy_lsp.analysis.light_mode_extractor import extract_requirements_light
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
                except OSError as exc:
                    logger.warning("Skipping unreadable file %s: %s", rel_path, exc)
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

            total = len(graph.requirements) + len(graph.actions) + len(graph.state_vars)
            logger.info(
                "Built requirement graph: %d requirements, %d actions, "
                "%d state vars, %d edges",
                len(graph.requirements),
                len(graph.actions),
                len(graph.state_vars),
                len(graph.edges),
            )

            # --- Populate SemanticModel with the same data (compatibility bridge) ---
            # The SemanticModel may already be built (via _get_model); if so, mirror
            # the RequirementGraph nodes and edges into it.  If the model hasn't been
            # built yet, we skip — the requirement graph remains the source of truth
            # until the model is constructed.
            _populate_semantic_model_from_graph(graph)

            return graph if total > 0 else None
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

    ctx = ToolContext(
        root=root,
        staging_dir=staging_dir,
        executor=executor,
        base_path=base_path,
    )
    # Wire up callables that close over start_mcp's local state
    ctx.find_ivy_files = _find_ivy_files
    ctx.get_model = _get_model
    ctx.get_model_status = _get_model_status
    ctx.get_req_graph = _get_req_graph
    ctx.make_viz_server_proxy = _make_viz_server_proxy
    ctx.get_basename_cache = _get_basename_cache
    ctx.make_resolve_callback = _make_resolve_callback

    from ivy_lsp.tools import register_all_tools

    register_all_tools(mcp, ctx)

    # --- Start or return ---

    if _return_app:
        return mcp

    logger.info("Starting ivy-lsp MCP server (workspace: %s)", root)
    mcp.run(transport="stdio")
