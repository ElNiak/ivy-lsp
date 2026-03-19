"""Entry point for running the Ivy Language Server.

Supports three modes:
  - Default:     LSP over stdio + MCP HTTP sidecar (unified process)
  - --mcp:       Standalone MCP server over stdio (backward compat)
  - --lsp-only:  LSP over stdio without MCP sidecar
"""

import logging
import os
import sys
from typing import Any


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


def main():
    """Start the Ivy Language Server in LSP, MCP, or unified mode."""
    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    log = logging.getLogger("ivy_lsp")

    if "--mcp" in sys.argv:
        # Standalone MCP server mode (backward compat): stdio transport
        try:
            from ivy_lsp.mcp_server import start_mcp
            from ivy_lsp.workspace_detection import detect_ivy_workspace

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
