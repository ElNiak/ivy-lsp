"""Entry point for running the Ivy Language Server.

Supports two modes:
  - LSP mode (default): Language Server Protocol over stdio
  - MCP mode (--mcp):   Model Context Protocol server exposing Ivy tools
"""

import logging
import os
import sys


def main():
    """Start the Ivy Language Server in LSP or MCP mode."""
    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    log = logging.getLogger("ivy_lsp")

    if "--mcp" in sys.argv:
        # MCP server mode: expose Ivy tools via Model Context Protocol
        try:
            from ivy_lsp.mcp_server import start_mcp

            workspace = None
            for i, arg in enumerate(sys.argv):
                if arg == "--workspace" and i + 1 < len(sys.argv):
                    workspace = sys.argv[i + 1]
            start_mcp(workspace_root=workspace)
        except ImportError as e:
            log.critical(
                "MCP mode requires the 'mcp' package: %s\n"
                "Install with: pip install ivy-lsp[mcp]",
                e,
            )
            sys.exit(1)
        except Exception as e:
            log.critical("Ivy MCP server crashed: %s", e, exc_info=True)
            sys.exit(1)
    else:
        # LSP server mode (default): Language Server Protocol over stdio
        try:
            from ivy_lsp.server import IvyLanguageServer

            server = IvyLanguageServer()
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
