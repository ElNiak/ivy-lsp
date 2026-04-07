"""Process utilities for multi-process worker pools."""

from __future__ import annotations

import sys


def worker_init(parent_sys_path: list[str]) -> None:
    """Initialize worker process with parent's sys.path.

    ``ProcessPoolExecutor`` with the ``spawn`` start method creates fresh
    Python interpreters that may not inherit the parent's ``sys.path``
    (especially when running under ``uvx`` or similar tools).
    """
    new_paths = [p for p in parent_sys_path if p not in sys.path]
    sys.path[0:0] = new_paths
