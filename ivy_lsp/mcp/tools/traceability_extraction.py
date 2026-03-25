"""Requirement extraction and manifest tool handlers.

Extracted from traceability.py to keep that module focused on coverage
analysis (ivy_coverage dispatcher and sub-functions).

Tools registered here:
- ivy_extract_requirements  – parse RFC text for MUST/SHOULD/MAY requirements
- ivy_manifest              – discover, validate, and refresh requirement manifests
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal

from ivy_lsp.infra.observability import ToolTraceContext
from ivy_lsp.mcp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared regex for RFC normative language extraction
# ---------------------------------------------------------------------------

_RFC_REQ_PATTERN = re.compile(
    r"([^.]*?\b(MUST NOT|MUST|SHALL NOT|SHALL|SHOULD NOT|SHOULD|"
    r"MAY|REQUIRED|RECOMMENDED|OPTIONAL)\b[^.]*\.)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Extraction / manifest logic (no ctx dependency)
# ---------------------------------------------------------------------------


async def _ivy_extract_requirements_logic(rfc_text: str) -> dict:
    """Parse RFC text to extract MUST/SHOULD/MAY structured requirements."""
    results = []
    for m in _RFC_REQ_PATTERN.finditer(rfc_text):
        text = m.group(1).strip()
        level = m.group(2)
        # Normalize level
        if level in ("SHALL", "REQUIRED"):
            level = "MUST"
        elif level in ("SHALL NOT",):
            level = "MUST NOT"
        elif level in ("RECOMMENDED",):
            level = "SHOULD"
        elif level in ("OPTIONAL",):
            level = "MAY"

        results.append(
            {
                "text": text,
                "level": level,
                "offset": m.start(),
            }
        )

    return {
        "requirements": results,
        "total": len(results),
        "by_level": {
            level: sum(1 for r in results if r["level"] == level)
            for level in sorted({r["level"] for r in results})
        },
    }


async def _ivy_generate_manifest(
    rfc_name: str,
    rfc_text: str,
    protocol: str = "",
    base_section: str = "",
) -> dict:
    """Generate a YAML requirements manifest from RFC text."""
    results = []
    for m in _RFC_REQ_PATTERN.finditer(rfc_text):
        text = m.group(1).strip()
        level = m.group(2)
        if level in ("SHALL", "REQUIRED"):
            level = "MUST"
        elif level in ("SHALL NOT",):
            level = "MUST NOT"
        elif level in ("RECOMMENDED",):
            level = "SHOULD"
        elif level in ("OPTIONAL",):
            level = "MAY"
        results.append({"text": text, "level": level, "offset": m.start()})

    rfc_lower = rfc_name.lower().replace(" ", "")
    manifest_lines = [
        f"rfc: {rfc_name}",
        f"title: '{protocol.upper()} protocol requirements'",
        "requirements:",
    ]
    for i, req in enumerate(results, start=1):
        section = f"{base_section}.{i}" if base_section else str(i)
        tag = f"{rfc_lower}:{section}"
        escaped_text = req["text"].replace("'", "''")
        manifest_lines.append(f"  {tag}:")
        manifest_lines.append(f"    text: '{escaped_text}'")
        manifest_lines.append(f"    section: '{section}'")
        manifest_lines.append(f"    level: {req['level']}")
        manifest_lines.append(f"    layer: ''")
        manifest_lines.append(f"    testable: true")

    yaml_content = "\n".join(manifest_lines) + "\n"
    suggested_path = ""
    if protocol:
        suggested_path = (
            f"protocol-testing/{protocol}/" f"{rfc_lower}_requirements.yaml"
        )

    return {
        "yaml": yaml_content,
        "total_requirements": len(results),
        "suggested_path": suggested_path,
        "by_level": {
            level: sum(1 for r in results if r["level"] == level)
            for level in sorted({r["level"] for r in results})
        },
    }


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------


def register_extraction_tools(mcp: Any, ctx: Any) -> None:
    """Register ivy_extract_requirements and ivy_manifest on *mcp*."""

    @mcp.tool()
    @safe_tool
    async def ivy_extract_requirements(
        rfc_text: str = "",
        output: str = "structured",
        rfc_name: str = "",
        protocol: str = "",
        base_section: str = "",
        rfc_source: str = "",
        sections: str = "",
    ) -> dict:
        """Parse RFC text to extract MUST/SHOULD/MAY requirements.

        Can output either structured requirement data or a YAML manifest
        ready for traceability tools.

        Args:
            rfc_text: Raw RFC text to parse for normative requirements.
                Can be empty when rfc_source is provided.
            output: Output format.
                - "structured": Extracted requirements as JSON with text,
                  level, offset, and by_level counts (default).
                - "manifest": YAML requirements manifest ready for
                  traceability tools. Requires rfc_name.
            rfc_name: RFC identifier (e.g., "RFC9000"). Required for
                output="manifest".
            protocol: Protocol name for layer inference (e.g., "quic").
                Used by output="manifest" for suggested path.
            base_section: Default section prefix (e.g., "4"). Used by
                output="manifest" for requirement IDs.
            rfc_source: RFC source to fetch text from. Accepts:
                - RFC number: "RFC9000" or "9000"
                - Internet draft: "draft-ietf-quic-transport-34"
                - Local file path: "/path/to/rfc.txt"
                - Direct URL: "https://example.com/rfc.txt"
                When provided, fetches text automatically (rfc_text ignored).
            sections: Comma-separated section numbers to extract from
                (e.g., "4,5,7.1"). Only used with rfc_source.
        """
        logger.debug(
            "[ivy_extract_requirements] workspace=%s, args=%r",
            ctx.root,
            {
                "output": output,
                "rfc_name": rfc_name,
                "protocol": protocol,
                "rfc_source": rfc_source,
                "sections": sections,
                "rfc_text_len": len(rfc_text),
            },
        )
        _tc = ToolTraceContext(
            "ivy_extract_requirements",
            {
                "output": output,
                "rfc_name": rfc_name,
                "rfc_source": rfc_source,
                "rfc_text_len": len(rfc_text),
            },
        )

        fetch_result = None
        # If rfc_source is provided, fetch and optionally filter by section
        if rfc_source:
            try:
                from ivy_lsp.core.rfc.fetcher import fetch_rfc
                from ivy_lsp.core.rfc.parser import get_section_text, parse_rfc_text

                fetch_result = await fetch_rfc(rfc_source)
                parsed = parse_rfc_text(fetch_result.text)

                # Auto-detect rfc_name if not provided
                if not rfc_name and parsed.rfc_number:
                    rfc_name = f"RFC{parsed.rfc_number}"

                # Filter by sections if specified
                if sections:
                    section_list = [s.strip() for s in sections.split(",") if s.strip()]
                    rfc_text = get_section_text(parsed, section_list)
                else:
                    rfc_text = fetch_result.text

            except Exception as exc:
                return _tc.finish(error_response(f"Failed to fetch RFC: {exc}"))

        if not rfc_text:
            return _tc.finish(
                error_response("Either rfc_text or rfc_source must be provided")
            )

        if output == "manifest":
            if not rfc_name:
                return _tc.finish(
                    error_response("rfc_name is required for output='manifest'")
                )
            result_data = await _ivy_generate_manifest(
                rfc_name, rfc_text, protocol, base_section
            )
            # Add metadata if we fetched the source
            if fetch_result:
                import datetime

                result_data["metadata"] = {
                    "generated_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "generator_version": "ivy-lsp",
                    "source": fetch_result.source,
                    "content_hash": fetch_result.content_hash,
                }
            return _tc.finish(result_data)
        else:  # default: structured
            return _tc.finish(await _ivy_extract_requirements_logic(rfc_text))

    # --- ivy_manifest tool ---

    @mcp.tool()
    @safe_tool
    async def ivy_manifest(
        mode: Literal["info", "validate", "staleness", "refresh"] = "info",
        protocol: str = "",
        rfc_source: str = "",
        check_online: bool = False,
    ) -> dict:
        """Manage and inspect requirement manifests.

        Args:
            mode: Operation mode.
                - "info": Discover all manifests, summarize each, detect
                  protocols without manifests.
                - "validate": Run validation on discovered manifests.
                  Reports missing fields, invalid levels, etc.
                - "staleness": Check if manifests are up-to-date
                  relative to their source RFCs.
                - "refresh": Fetch RFC source, extract requirements,
                  and diff against current manifest by ID.
            protocol: Protocol name to filter (e.g. "quic").
            rfc_source: RFC source for refresh mode.
            check_online: Whether to check RFC editor API for
                staleness (obsolescence, updates, errata).
        """
        logger.debug(
            "[ivy_manifest] workspace=%s, args=%r",
            ctx.root,
            {"mode": mode, "protocol": protocol, "rfc_source": rfc_source},
        )
        _tc = ToolTraceContext(
            "ivy_manifest",
            {"mode": mode, "protocol": protocol, "rfc_source": rfc_source},
        )

        from ivy_lsp.core.semantic.rfc_annotations import (
            find_manifests,
            load_manifest_with_metadata,
            validate_manifest,
        )

        workspace_root = ctx.root
        all_manifests = find_manifests(workspace_root)

        # Filter by protocol if specified
        if protocol:
            prot_filter = f"protocol-testing/{protocol}/"
            all_manifests = [m for m in all_manifests if prot_filter in m]

        if mode == "info":
            summaries = []
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                # Infer protocol from path
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )
                prot = ""
                if "protocol-testing/" in rel:
                    parts = rel.split("protocol-testing/")[1].split("/")
                    if parts:
                        prot = parts[0]
                summaries.append(
                    {
                        "path": rel,
                        "protocol": prot,
                        "requirements": len(result.requirements),
                        "has_metadata": result.metadata is not None,
                        "warnings": len(result.warnings),
                    }
                )

            # Detect protocols without manifests
            pt_dir = os.path.join(workspace_root, "protocol-testing")
            protocols_with_manifests = {
                s["protocol"] for s in summaries if s["protocol"]
            }
            protocols_without = []
            if os.path.isdir(pt_dir):
                for entry in os.listdir(pt_dir):
                    if os.path.isdir(os.path.join(pt_dir, entry)):
                        if entry not in protocols_with_manifests:
                            protocols_without.append(entry)

            return _tc.finish(
                {
                    "manifests": summaries,
                    "total_manifests": len(summaries),
                    "protocols_without_manifests": protocols_without,
                }
            )

        elif mode == "validate":
            import yaml

            results = []
            for mpath in all_manifests:
                try:
                    with open(mpath, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                except Exception as exc:
                    results.append(
                        {
                            "path": mpath,
                            "warnings": [f"Failed to load: {exc}"],
                        }
                    )
                    continue

                warnings = (
                    validate_manifest(data)
                    if isinstance(data, dict)
                    else ["Not a mapping"]
                )
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )
                results.append(
                    {
                        "path": rel,
                        "warnings": warnings,
                        "valid": len(warnings) == 0,
                    }
                )

            return _tc.finish(
                {
                    "results": results,
                    "total_manifests": len(results),
                    "all_valid": all(r.get("valid", False) for r in results),
                }
            )

        elif mode == "staleness":
            from ivy_lsp.core.rfc.staleness import check_staleness

            reports = []
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                rel = (
                    os.path.relpath(mpath, workspace_root) if workspace_root else mpath
                )

                if result.metadata is None:
                    reports.append(
                        {
                            "path": rel,
                            "status": "no_metadata",
                            "info": ["No metadata section; cannot check staleness."],
                        }
                    )
                    continue

                meta = result.metadata
                rfc_num = ""
                rfc_field = ""
                # Try to extract RFC number from the rfc field
                for rpath in all_manifests:
                    if rpath == mpath:
                        try:
                            with open(rpath, encoding="utf-8") as f:
                                import yaml

                                rdata = yaml.safe_load(f)
                            rfc_field = (
                                str(rdata.get("rfc", ""))
                                if isinstance(rdata, dict)
                                else ""
                            )
                        except Exception:
                            pass
                        break

                import re as _re

                rfc_match = _re.search(r"(\d+)", rfc_field)
                if rfc_match:
                    rfc_num = rfc_match.group(1)

                report = await check_staleness(
                    manifest_source=meta.source,
                    manifest_hash=meta.content_hash,
                    rfc_number=rfc_num,
                    check_online=check_online,
                )
                reports.append(
                    {
                        "path": rel,
                        "is_stale": report.is_stale,
                        "reasons": report.reasons,
                        "info": report.info,
                        "content_hash_match": report.content_hash_match,
                        "obsoleted_by": report.obsoleted_by,
                        "updated_by": report.updated_by,
                    }
                )

            return _tc.finish({"reports": reports})

        elif mode == "refresh":
            if not rfc_source:
                return _tc.finish(
                    error_response("rfc_source is required for mode='refresh'")
                )

            # Fetch and extract new requirements
            try:
                from ivy_lsp.core.rfc.fetcher import fetch_rfc

                fetch_result = await fetch_rfc(rfc_source)

                # Extract requirements from fetched text
                new_reqs = await _ivy_extract_requirements_logic(fetch_result.text)
            except Exception as exc:
                return _tc.finish(error_response(f"Failed to fetch/parse: {exc}"))

            # Load current manifest requirements
            current_ids: set[str] = set()
            for mpath in all_manifests:
                result = load_manifest_with_metadata(mpath)
                current_ids.update(result.requirements.keys())

            return _tc.finish(
                {
                    "rfc_source": rfc_source,
                    "new_requirements_found": new_reqs.get("total", 0),
                    "current_manifest_ids": len(current_ids),
                    "by_level": new_reqs.get("by_level", {}),
                    "source_hash": (fetch_result.content_hash if fetch_result else ""),
                }
            )

        return _tc.finish(error_response(f"Unknown mode '{mode}'"))
