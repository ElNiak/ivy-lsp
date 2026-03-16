"""RFC annotation parsing and requirement manifest loading.

Parses bracket-tag comments from Ivy source files and loads YAML
requirement manifests to enable RFC traceability and coverage analysis.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ivy_lsp.semantic.nodes import RfcAnnotation, RfcRequirement

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^\w+(?:[.:]\w+)*$")  # e.g. "rfc9000", "rfc9000:4.1", "4.1", "4"
_BRACKET_RE = re.compile(
    r"#\s*\[([\w:.,\s]+)\]\s*$"
)  # e.g. "# [rfc9000:4.1, rfc9000:8.1]"


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------


def normalize_tag_to_manifest_ids(
    tag: str,
    manifest_keys: set[str],
) -> set[str]:
    """Expand a bracket tag to matching manifest IDs.

    Supports:
    - Exact: 'rfc9000:4.1' -> {'rfc9000:4.1'}
    - Qualified prefix: 'rfc9000:4' -> {'rfc9000:4.1', 'rfc9000:4.6'}
    - Bare numeric: '4' -> {'rfc9000:4.1', 'rfc9000:4.6'} (all rfc:4.*)
    - Bare section: '4.1' -> {'rfc9000:4.1'}
    """
    if tag in manifest_keys:
        return {tag}

    matches: set[str] = set()

    # Extract RFC prefixes from manifest keys
    rfc_prefixes: set[str] = set()
    for key in manifest_keys:
        if ":" in key:
            rfc_prefixes.add(key.split(":")[0])

    # Qualified with prefix: "rfc9000:4" -> match "rfc9000:4.*"
    if ":" in tag:
        for key in manifest_keys:
            if key == tag or key.startswith(tag + "."):
                matches.add(key)
        return matches

    # Bare numeric/section: "4" or "4.1" -> try all RFC prefixes
    for prefix in rfc_prefixes:
        exact = f"{prefix}:{tag}"
        if exact in manifest_keys:
            matches.add(exact)
        for key in manifest_keys:
            if key.startswith(f"{prefix}:{tag}."):
                matches.add(key)

    return matches


def parse_rfc_tags(line_text: str) -> List[str]:
    """Parse RFC bracket tags from a single line of source.

    Supports comma-separated tags: ``# [rfc9000:4.1, rfc9000:8.1]``

    Returns list of validated tag strings.

    Lines that are commented-out code (e.g. ``#require foo # [8]``) are
    skipped to avoid false-positive tag matches.  Pure tag-only comments
    like ``# [8]`` are still parsed.
    """
    stripped = line_text.strip()
    # Filter out commented-out code lines that happen to contain a tag.
    # If the line starts with '#', it must be a pure tag comment (the entire
    # line is just "# [tags]") to be parsed.  Commented-out code like
    # "#require foo # [8]" is rejected because _BRACKET_RE won't match
    # from the start of such a line.
    if stripped.startswith("#") and not _BRACKET_RE.match(stripped):
        return []
    m = _BRACKET_RE.search(line_text)
    if not m:
        return []
    raw = m.group(1)
    candidates = [t.strip() for t in raw.split(",") if t.strip()]
    return [t for t in candidates if _TAG_RE.match(t)]


def parse_file_rfc_annotations(source: str, filepath: str) -> List[RfcAnnotation]:
    """Scan all lines for bracket tags and return RfcAnnotation nodes."""
    annotations: List[RfcAnnotation] = []
    for i, line in enumerate(source.split("\n")):
        tags = parse_rfc_tags(line)
        if tags:
            annotations.append(
                RfcAnnotation(
                    id=f"{filepath}:{i}:0",
                    file=filepath,
                    line=i,
                    tags=tags,
                )
            )
    return annotations


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------


def load_requirement_manifest(path: str) -> Dict[str, RfcRequirement]:
    """Parse a YAML requirement manifest file.

    Expected structure::

        rfc: "RFC9000"
        requirements:
          rfc9000:4.1:
            text: "senders MUST NOT send data..."
            section: "4.1"
            level: MUST
            layer: frame
            testable: true
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; cannot load manifest %s", path)
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        logger.warning("Failed to load manifest %s", path, exc_info=True)
        return {}

    if not isinstance(data, dict):
        return {}

    rfc_name = data.get("rfc", "")
    reqs_data = data.get("requirements", {})
    if not isinstance(reqs_data, dict):
        return {}

    result: Dict[str, RfcRequirement] = {}
    for tag_id, req_data in reqs_data.items():
        if not isinstance(req_data, dict):
            continue
        result[str(tag_id)] = RfcRequirement(
            id=str(tag_id),
            rfc=str(rfc_name),
            section=str(req_data.get("section", "")),
            text=str(req_data.get("text", "")),
            level=str(req_data.get("level", "MUST")),
            layer=str(req_data.get("layer", "")),
            testable=bool(req_data.get("testable", True)),
        )

    return result


def find_manifests(workspace_root: str) -> List[str]:
    """Glob for ``*_requirements.yaml`` in ``protocol-testing/`` subdirectories."""
    results: List[str] = []
    pt_dir = os.path.join(workspace_root, "protocol-testing")
    if not os.path.isdir(pt_dir):
        return results
    for dirpath, _dirnames, filenames in os.walk(pt_dir):
        for fname in filenames:
            if fname.endswith("_requirements.yaml"):
                results.append(os.path.join(dirpath, fname))
    return results


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------


@dataclass
class CoverageStats:
    """Summary of RFC requirement coverage."""

    total: int = 0
    covered: int = 0
    by_level: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_layer: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def uncovered(self) -> int:
        """Return count of requirements not yet covered."""
        return self.total - self.covered


def compute_coverage(
    annotations: List[RfcAnnotation],
    requirements: Dict[str, RfcRequirement],
) -> CoverageStats:
    """Compute coverage of requirements by source annotations."""
    manifest_keys = set(requirements.keys())
    covered_ids: set[str] = set()
    for ann in annotations:
        for tag in ann.tags:
            covered_ids.update(normalize_tag_to_manifest_ids(tag, manifest_keys))
    covered_ids &= manifest_keys
    total = len(requirements)
    covered = len(covered_ids)

    by_level: Dict[str, Dict[str, int]] = {}
    by_layer: Dict[str, Dict[str, int]] = {}

    for req_id, req in requirements.items():
        is_covered = req_id in covered_ids

        if req.level not in by_level:
            by_level[req.level] = {"total": 0, "covered": 0}
        by_level[req.level]["total"] += 1
        if is_covered:
            by_level[req.level]["covered"] += 1

        if req.layer:
            if req.layer not in by_layer:
                by_layer[req.layer] = {"total": 0, "covered": 0}
            by_layer[req.layer]["total"] += 1
            if is_covered:
                by_layer[req.layer]["covered"] += 1

    return CoverageStats(
        total=total,
        covered=covered,
        by_level=by_level,
        by_layer=by_layer,
    )
