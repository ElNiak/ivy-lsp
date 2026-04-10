"""Environment detection utilities for native Ivy compilation."""

from __future__ import annotations

import functools
import os
import subprocess
import sys
from typing import Optional


@functools.lru_cache(maxsize=1)
def detect_z3_dir() -> Optional[str]:
    """Detect the Z3 installation directory for native compilation.

    Checks in order:
    1. Z3DIR environment variable
    2. Homebrew prefix on macOS (brew --prefix z3)
    3. /usr/local/include/z3++.h
    4. /usr/include/z3++.h

    Returns:
        Path to the Z3 installation root, or None if not found.
    """
    z3dir = os.environ.get("Z3DIR")
    if z3dir and os.path.isdir(z3dir):
        return z3dir

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["brew", "--prefix", "z3"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                brew_path = result.stdout.strip()
                if brew_path and os.path.isdir(brew_path):
                    return brew_path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    for prefix in ("/usr/local", "/usr"):
        if os.path.isfile(os.path.join(prefix, "include", "z3++.h")):
            return prefix

    return None
