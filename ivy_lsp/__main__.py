"""Entry point for running the Ivy Language Server over stdio."""

import logging
import os
import sys


def main():
    """Start the Ivy Language Server over stdio."""
    log_level = os.environ.get("IVY_LSP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    log = logging.getLogger("ivy_lsp")
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
