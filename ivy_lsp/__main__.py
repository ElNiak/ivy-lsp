"""Entry point for running the Ivy Language Server.

Supports three modes:
  - Default:     LSP over stdio + MCP HTTP sidecar (unified process)
  - --mcp:       Standalone MCP server over stdio (backward compat)
  - --lsp-only:  LSP over stdio without MCP sidecar
"""

import logging
import os
import signal
import sys
import threading
import time
from typing import Any

from ivy_lsp.observability import (
    LogCategory,
    call_context,
    enable_package_instrumentation,
    log_phase,
)


def _fixed_params_hook(obj: dict, cls: type) -> Any:
    """Structure hook that handles optional ``params`` per JSON-RPC 2.0.

    pygls 2.0.1 ships a ``_params_field_structure_hook`` that crashes when
    the incoming JSON-RPC message omits the ``params`` field (which the
    JSON-RPC 2.0 spec explicitly allows).  This replacement sets
    ``params=None`` when the field is absent so that
    ``JsonRPCRequestMessage`` / ``JsonRPCNotification`` can be constructed.
    """
    from pygls.protocol import _dict_to_object

    if "params" in obj:
        obj["params"] = _dict_to_object(obj["params"])
    else:
        obj["params"] = None
    return cls(**obj)


def _patch_pygls_converter(server: Any) -> None:
    """Re-register structure hooks on *server*'s protocol converter."""
    from pygls.protocol.json_rpc import JsonRPCNotification, JsonRPCRequestMessage

    converter = server.protocol._converter
    converter.register_structure_hook(JsonRPCRequestMessage, _fixed_params_hook)
    converter.register_structure_hook(JsonRPCNotification, _fixed_params_hook)


def _parse_mcp_port() -> int:
    """Parse MCP port from --mcp-port arg or IVY_MCP_PORT env var."""
    for i, arg in enumerate(sys.argv):
        if arg == "--mcp-port" and i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                pass
    env_port = os.environ.get("IVY_MCP_PORT", "")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            pass
    return 0  # use default (19847)


def _setup_log_rotation() -> str:
    """Add a rotating file handler for MCP/LSP server logs.

    Writes to the session log directory when available, falling back to
    ``/tmp/ivy-lsp.log``.  Returns the resolved log path.

    Env vars:
      - ``IVY_LSP_LOG_FILE``: log path (overrides session-aware resolution)
      - ``IVY_LSP_LOG_MAX_BYTES``: max bytes per file (default 10 MB)
      - ``IVY_LSP_LOG_BACKUP_COUNT``: rotated backup count (default 3)
    """
    from logging.handlers import RotatingFileHandler

    log_path = os.environ.get("IVY_LSP_LOG_FILE", "")
    if not log_path:
        try:
            from ivy_lsp.observability.session import (
                get_session_id,
                resolve_session_log_dir,
            )

            session_dir = resolve_session_log_dir(get_session_id())
            session_dir.mkdir(parents=True, exist_ok=True)
            log_path = str(session_dir / "ivy-lsp.log")
        except Exception:
            log_path = "/tmp/ivy-lsp.log"
    try:
        max_bytes = int(os.environ.get("IVY_LSP_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    except (ValueError, TypeError):
        max_bytes = 10 * 1024 * 1024
    try:
        backup_count = int(os.environ.get("IVY_LSP_LOG_BACKUP_COUNT", "3"))
    except (ValueError, TypeError):
        backup_count = 3
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    return log_path


def main():
    """Start the Ivy Language Server in LSP, MCP, or unified mode."""
    startup_t0 = time.perf_counter()
    startup_call_id = f"startup-{os.getpid()}"
    with call_context(startup_call_id):
        _main_impl(startup_t0)


def _main_impl(startup_t0: float) -> None:
    """Internal startup implementation with correlation context attached."""
    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    # Subcommand dispatch: index, detect — lightweight CLI commands that
    # don't need log rotation, debug tracing, or SIGTERM handling.
    if len(sys.argv) > 1 and sys.argv[1] == "index":
        from ivy_lsp.index_builder import cli_index

        sys.exit(cli_index(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "detect":
        import json

        from ivy_lsp.workspace.context import WorkspaceContext

        start = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        print(json.dumps(WorkspaceContext.detect(start), indent=2))
        sys.exit(0)

    # --- Full server setup below (LSP/MCP modes only) ---
    log = logging.getLogger("ivy_lsp")

    # Add rotating file handler to prevent unbounded log growth
    resolved_log_path = _setup_log_rotation()

    # Demote stderr to WARNING since the rotating file is the primary log sink.
    # Without this, every log line appears twice (stderr + file) when the MCP
    # process's stderr is redirected to the same log destination.
    for _h in logging.getLogger().handlers:
        if (
            isinstance(_h, logging.StreamHandler)
            and not isinstance(_h, logging.FileHandler)
            and getattr(_h, "stream", None) is sys.stderr
        ):
            _h.setLevel(logging.WARNING)
            break

    log_phase(
        log,
        category=LogCategory.MILESTONE,
        phase="startup",
        message="Rotating log configured",
        data={"log_file": resolved_log_path},
        level=logging.INFO,
    )

    # Add dedup filter to suppress cascading duplicate messages
    from ivy_lsp.observability import DedupFilter

    logging.getLogger().addFilter(DedupFilter())

    # Initialize debug tracer if enabled
    from ivy_lsp.config import get_config

    cfg = get_config()
    if cfg.debug_log:
        from ivy_lsp.observability import init_tracer

        ws_root = cfg.workspace or cfg.workspace_root or os.getcwd()
        tracer = init_tracer(
            workspace_root=ws_root,
            log_path=cfg.debug_log_path,
        )
        log.info("Debug tracing enabled: %s", tracer.log_path)

    try:
        from ivy_lsp.observability import (
            get_session_logger,
            install_session_jsonl_handler,
        )

        install_session_jsonl_handler()
        session_logger = get_session_logger()
        log_phase(
            log,
            category=LogCategory.MILESTONE,
            phase="startup",
            message="Observability paths ready",
            data={
                "events_file": str(session_logger.events_file),
                "debug_log_enabled": cfg.debug_log,
                "debug_log_path": cfg.debug_log_path,
                "elapsed_ms": round((time.perf_counter() - startup_t0) * 1000, 2),
            },
            level=logging.INFO,
        )
    except Exception:
        log.debug("Session logger initialization skipped", exc_info=True)

    if cfg.trace_all_functions:
        summary = enable_package_instrumentation(
            "ivy_lsp",
            category=LogCategory.ACTIVITY,
            phase="deep-trace",
            channel="core",
        )
        log_phase(
            log,
            category=LogCategory.MILESTONE,
            phase="startup",
            message="Deep tracing enabled for ivy_lsp package",
            data={
                "trace_all_functions": True,
                "modules_scanned": summary.get("modules", 0),
                "wrapped_callables": summary.get("wrapped", 0),
            },
            level=logging.INFO,
        )

    def _sigterm_handler(signum, frame):
        log.warning(
            "[SIGTERM] Server received signal %d (PID=%d). Shutting down.",
            signum,
            os.getpid(),
        )
        logging.shutdown()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm_handler)

    # Parent-process watchdog: detect when Claude Code (our parent) dies.
    # On macOS/Linux, an orphaned process is reparented to PID 1 (launchd/init).
    # When this happens, the stdio pipes are broken and the server is useless.
    _parent_pid = os.getppid()

    def _parent_watchdog():
        import time

        while True:
            time.sleep(5)
            current_ppid = os.getppid()
            if current_ppid != _parent_pid:
                log.warning(
                    "[ORPHAN] Parent PID changed %d -> %d (parent died). "
                    "Shutting down.",
                    _parent_pid,
                    current_ppid,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return

    _watchdog = threading.Thread(target=_parent_watchdog, daemon=True)
    _watchdog.start()

    if "--mcp" in sys.argv:
        # Standalone MCP server mode (backward compat): stdio transport
        try:
            from ivy_lsp.mcp_server import start_mcp
            from ivy_lsp.workspace.detection import detect_ivy_workspace

            workspace = None
            docker_image = os.environ.get("IVY_DOCKER_IMAGE")
            base_path = os.environ.get("IVY_BASE_PATH")
            staging_dir = None

            for i, arg in enumerate(sys.argv):
                if arg == "--workspace" and i + 1 < len(sys.argv):
                    workspace = sys.argv[i + 1]
                elif arg == "--docker-image" and i + 1 < len(sys.argv):
                    docker_image = sys.argv[i + 1]
                elif arg == "--base-path" and i + 1 < len(sys.argv):
                    base_path = sys.argv[i + 1]
                elif arg == "--staging-dir" and i + 1 < len(sys.argv):
                    staging_dir = sys.argv[i + 1]

            # Auto-detect workspace scope
            ws_config = detect_ivy_workspace(
                start_dir=workspace or os.getcwd(),
                explicit_workspace=workspace,
            )
            log.info(
                "Workspace detection: root=%s, detected_by=%s, type=%s",
                ws_config.workspace_root,
                ws_config.detected_by,
                ws_config.project_type,
            )
            start_mcp(
                workspace_root=ws_config.workspace_root,
                ws_config=ws_config,
                docker_image=docker_image,
                base_path=base_path,
                staging_dir=staging_dir,
            )
        except ImportError as e:
            log.critical(
                "[MCP-FATAL] Missing dependency: %s\n"
                "Install with: pip install ivy-lsp[mcp]",
                e,
            )
            sys.exit(1)
        except Exception as e:
            log.critical("[MCP-FATAL] Ivy MCP server crashed: %s", e, exc_info=True)
            sys.exit(1)
    else:
        # LSP server mode (default): stdio + optional MCP HTTP sidecar
        try:
            from ivy_lsp.server import IvyLanguageServer

            server = IvyLanguageServer()
            _patch_pygls_converter(server)

            # Start MCP HTTP sidecar unless --lsp-only is passed
            if "--lsp-only" not in sys.argv:
                mcp_port = _parse_mcp_port()
                server.start_mcp_sidecar(port=mcp_port)

            server.start_io()
        except ImportError as e:
            log.critical(
                "Failed to start Ivy Language Server: missing dependency: %s",
                e,
            )
            sys.exit(1)
        except Exception as e:
            log.critical(
                "Ivy Language Server crashed: %s",
                e,
                exc_info=True,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
