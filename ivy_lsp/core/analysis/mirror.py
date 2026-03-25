"""First-class NCT mirror model.

A mirror represents an endpoint test entry point in the NCT methodology.
It captures the transitive include closure, exported/imported actions,
and tester role for a single test file.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional

logger = logging.getLogger(__name__)


class MirrorRole(str, Enum):
    """Tester role in an NCT mirror (accounts for role inversion)."""

    CLIENT = "client"
    SERVER = "server"
    MIM = "mim"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MirrorId:
    """Stable, hashable identity for an NCT mirror.

    Combines protocol + entry point basename for a globally unique key.
    """

    protocol: str
    entry_stem: str

    def __str__(self) -> str:  # noqa: D105
        return f"{self.protocol}::{self.entry_stem}"

    @classmethod
    def from_test_file(cls, test_file: str, protocol: str) -> MirrorId:
        """Create a MirrorId from a test file path and protocol name."""
        stem = os.path.basename(test_file).removesuffix(".ivy")
        return cls(protocol=protocol, entry_stem=stem)
