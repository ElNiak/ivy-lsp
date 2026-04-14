"""RFC annotation parsing and requirement manifest loading.

Parses bracket-tag comments from Ivy source files and loads YAML
requirement manifests to enable RFC traceability and coverage analysis.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

from ivy_lsp.core.semantic.nodes import ManifestMetadata, RfcAnnotation, RfcRequirement

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^\w+(?:[.:]\w+)*$")  # e.g. "rfc9000", "rfc9000:4.1", "4.1", "4"
_BRACKET_RE = re.compile(
    r"#\s*\[([\w:.,\s]+)\]\s*$"
)  # e.g. "# [rfc9000:4.1, rfc9000:8.1]"
_BARE_NUMERIC_RE = re.compile(r"^\d+$")  # e.g. "4", "12" — no dots, no prefix


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


def is_tag_covered(tag: str, manifest_keys: set[str]) -> bool:
    """Check whether a bracket tag resolves to at least one manifest requirement.

    Wraps ``normalize_tag_to_manifest_ids`` for use in orphan-detection code
    paths, ensuring consistency between coverage computation and orphan
    diagnostics.
    """
    return bool(normalize_tag_to_manifest_ids(tag, manifest_keys))


def _is_rfc_annotation(line_text: str, tags: List[str]) -> bool:
    """Determine whether a bracket-tag comment is a genuine RFC annotation.

    Rejects bare numeric tags (e.g. ``# [1]``, ``# [42]``) when they appear
    on lines that contain code before the bracket comment.  Such tags are
    typically array indices or struct field markers, not RFC section
    references.

    Returns ``True`` if the tags should be treated as RFC annotations.
    """
    if not tags:
        return False
    # If any tag is *not* a bare numeric, keep the annotation
    if not all(_BARE_NUMERIC_RE.match(t) for t in tags):
        return True
    # All tags are bare numerics — check if there is code before the bracket
    m = _BRACKET_RE.search(line_text)
    if m is None:
        return False  # defensive: shouldn't happen since tags were parsed
    before_bracket = line_text[: m.start()].rstrip()
    # Pure comment lines like "# [8]" have only whitespace/# before bracket
    stripped = before_bracket.lstrip()
    if stripped == "" or stripped == "#":
        # Standalone comment with all bare integers (no dots/colons)
        # → reject as field marker, not RFC annotation
        return False
    # Code exists before bracket — reject bare numerics as phantom tags
    return False


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
    """Scan all lines for bracket tags and return RfcAnnotation nodes.

    Uses ``_is_rfc_annotation()`` to filter out bare numeric tags on code
    lines (e.g. array indices like ``payload : frame.arr # [8]``).
    """
    annotations: List[RfcAnnotation] = []
    for i, line in enumerate(source.split("\n")):
        tags = parse_rfc_tags(line)
        if tags and _is_rfc_annotation(line, tags):
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

    # Validate manifest and log warnings
    manifest_warnings = validate_manifest(data)
    for warning in manifest_warnings:
        logger.warning("Manifest %s: %s", path, warning)

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
            level=cast(Any, str(req_data.get("level", "MUST"))),
            layer=str(req_data.get("layer", "")),
            testable=bool(req_data.get("testable", True)),
        )

    return result


# ---------------------------------------------------------------------------
# Manifest with metadata
# ---------------------------------------------------------------------------


@dataclass
class ManifestLoadResult:
    """Result of loading a manifest with metadata."""

    requirements: Dict[str, RfcRequirement] = field(default_factory=dict)
    metadata: Optional[ManifestMetadata] = None
    warnings: List[str] = field(default_factory=list)
    path: str = ""


def load_manifest_with_metadata(path: str) -> ManifestLoadResult:
    """Load a manifest with optional metadata section.

    Parses the ``metadata:`` key from the YAML if present. Runs
    ``validate_manifest()`` and collects warnings. The signature of
    ``load_requirement_manifest()`` is unchanged (9 call sites depend
    on it returning ``Dict[str, RfcRequirement]``).
    """
    try:
        import yaml
    except ImportError:
        return ManifestLoadResult(path=path, warnings=["PyYAML not installed"])

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as exc:
        return ManifestLoadResult(path=path, warnings=[f"Failed to load: {exc}"])

    if not isinstance(data, dict):
        return ManifestLoadResult(
            path=path, warnings=["Manifest root is not a mapping"]
        )

    warnings = validate_manifest(data)
    requirements = load_requirement_manifest(path)

    # Parse metadata if present
    metadata = None
    meta_data = data.get("metadata")
    if isinstance(meta_data, dict):
        metadata = ManifestMetadata(
            generated_at=str(meta_data.get("generated_at", "")),
            generator_version=str(meta_data.get("generator_version", "")),
            source=str(meta_data.get("source", "")),
            content_hash=str(meta_data.get("content_hash", "")),
            last_checked=str(meta_data.get("last_checked", "")),
            obsoleted_by=str(meta_data.get("obsoleted_by", "")),
            updated_by=str(meta_data.get("updated_by", "")),
            errata_ids=str(meta_data.get("errata_ids", "")),
            is_draft=bool(meta_data.get("is_draft", False)),
            draft_name=str(meta_data.get("draft_name", "")),
            draft_version=str(meta_data.get("draft_version", "")),
        )

    return ManifestLoadResult(
        requirements=requirements,
        metadata=metadata,
        warnings=warnings,
        path=path,
    )


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

_VALID_LEVELS = {"MUST", "MUST NOT", "SHOULD", "SHOULD NOT", "MAY"}
_LEVEL_SYNONYMS: Dict[str, str] = {
    "SHALL": "MUST",
    "SHALL NOT": "MUST NOT",
    "REQUIRED": "MUST",
    "RECOMMENDED": "SHOULD",
    "OPTIONAL": "MAY",
}


def validate_manifest(data: dict) -> List[str]:
    """Validate a parsed YAML manifest and return a list of warning messages.

    Checks:
    - ``rfc`` key is present and non-empty
    - ``requirements`` key is present and is a dict
    - Each requirement has ``text``, ``section``, ``level``
    - Levels are valid (or synonyms that will be normalized)
    - No duplicate requirement IDs (YAML allows overwrite)
    """
    warnings: List[str] = []

    if not isinstance(data, dict):
        warnings.append("Manifest root is not a mapping")
        return warnings

    if not data.get("rfc"):
        warnings.append("Missing or empty 'rfc' field")

    reqs = data.get("requirements")
    if reqs is None:
        warnings.append("Missing 'requirements' field")
        return warnings
    if not isinstance(reqs, dict):
        warnings.append("'requirements' field is not a mapping")
        return warnings

    seen_ids: set[str] = set()
    for req_id, req_data in reqs.items():
        str_id = str(req_id)
        if str_id in seen_ids:
            warnings.append(f"Duplicate requirement ID: {str_id}")
        seen_ids.add(str_id)

        if not isinstance(req_data, dict):
            warnings.append(f"{str_id}: requirement entry is not a mapping")
            continue

        if not req_data.get("text"):
            warnings.append(f"{str_id}: missing or empty 'text' field")
        if not req_data.get("section"):
            warnings.append(f"{str_id}: missing or empty 'section' field")

        level = str(req_data.get("level", "")).upper()
        if level in _LEVEL_SYNONYMS:
            warnings.append(
                f"{str_id}: level '{req_data.get('level')}' will be "
                f"normalized to '{_LEVEL_SYNONYMS[level]}'"
            )
        elif level and level not in _VALID_LEVELS:
            warnings.append(
                f"{str_id}: invalid level '{req_data.get('level')}'. "
                f"Valid levels: {sorted(_VALID_LEVELS)}"
            )

    return warnings


# ---------------------------------------------------------------------------
# Tag resolution diagnostics
# ---------------------------------------------------------------------------


@dataclass
class TagResolution:
    """Result of resolving a bracket tag against manifest keys, with warnings."""

    matched_ids: set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)


def normalize_tag_with_diagnostics(tag: str, manifest_keys: set[str]) -> TagResolution:
    """Resolve a tag to manifest IDs and collect ambiguity warnings.

    Like ``normalize_tag_to_manifest_ids`` but additionally warns when a
    bare numeric tag (e.g. ``4``) matches multiple RFC prefixes, which
    indicates potential ambiguity.
    """
    matched = normalize_tag_to_manifest_ids(tag, manifest_keys)
    warnings: List[str] = []

    if _BARE_NUMERIC_RE.match(tag) and matched:
        # Check if the tag matches keys under multiple RFC prefixes
        prefixes = {k.split(":")[0] for k in matched if ":" in k}
        if len(prefixes) > 1:
            warnings.append(
                f"Bare tag [{tag}] is ambiguous: matches requirements "
                f"under {sorted(prefixes)}"
            )

    return TagResolution(matched_ids=matched, warnings=warnings)


def find_manifests(workspace_root: str, protocol: str | None = None) -> List[str]:
    """Glob for ``*_requirements.yaml`` under ``protocol-testing/`` or the root itself.

    Args:
        workspace_root: Workspace root or a protocol directory inside
            ``protocol-testing/`` (e.g. from IndexBuilder per-protocol builds).
        protocol: When set, restrict search to ``protocol-testing/{protocol}/``
            so manifests from other protocols are excluded.
    """
    results: List[str] = []
    pt_dir = os.path.join(workspace_root, "protocol-testing")
    if os.path.isdir(pt_dir):
        if protocol:
            proto_dir = os.path.join(pt_dir, protocol)
            search_root = proto_dir if os.path.isdir(proto_dir) else pt_dir
        else:
            search_root = pt_dir
    else:
        # Fallback: root is already inside protocol-testing/ (IndexBuilder)
        search_root = workspace_root
    for dirpath, _dirnames, filenames in os.walk(search_root):
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
