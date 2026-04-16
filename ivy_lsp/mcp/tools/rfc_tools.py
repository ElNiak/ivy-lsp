# ivy_lsp/mcp/tools/rfc_tools.py
"""RFC lookup, search, and section analysis MCP tools."""

from __future__ import annotations

import logging
from typing import Any

from ivy_lsp.mcp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)


def register_rfc_tools(mcp: Any, ctx: Any) -> None:
    """Register RFC MCP tools."""

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_rfc_get(
        number: str,
        format: str = "full",
    ) -> dict:
        """Retrieve an RFC document by number.

        Fetches from local cache, disk cache, or IETF remote (in that order).

        Args:
            number: RFC number (e.g. "4271", "rfc9000") or draft ID.
            format: "full" (complete document), "metadata" (title/status only),
                    or "sections" (table of contents without body text).
        """
        if ctx.rfc_service is None:
            return error_response("RFC service not initialized.")

        if format not in ("full", "metadata", "sections"):
            return error_response(
                f"Unknown format '{format}'. Valid: full, metadata, sections."
            )

        try:
            doc = await ctx.rfc_service.get_rfc(number, format=format)
        except Exception as exc:
            return error_response(f"Failed to fetch RFC {number}: {exc}")

        result: dict[str, Any] = {
            "status": "ok",
            "number": doc.number,
            "title": doc.title,
            "format": format,
        }

        if format == "metadata":
            result["metadata"] = {
                "authors": doc.metadata.authors,
                "date": doc.metadata.date,
                "status": doc.metadata.status,
                "obsoletes": doc.metadata.obsoletes,
                "updates": doc.metadata.updates,
            }
        elif format == "sections":
            result["sections"] = [
                {"number": s.number, "title": s.title} for s in doc.sections
            ]
        else:
            result["sections"] = [
                {
                    "number": s.number,
                    "title": s.title,
                    "text": s.text,
                }
                for s in doc.sections
            ]

        return result

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_rfc_search(
        query: str,
        limit: int = 10,
    ) -> dict:
        """Search for RFCs by keyword via the IETF Datatracker API.

        Args:
            query: Search terms (e.g. "BGP path attributes").
            limit: Maximum number of results (default 10).
        """
        if ctx.rfc_service is None:
            return error_response("RFC service not initialized.")

        try:
            results = await ctx.rfc_service.search(query, limit=limit)
        except Exception as exc:
            return error_response(f"Search failed: {exc}")

        return {
            "status": "ok",
            "query": query,
            "count": len(results),
            "results": [
                {
                    "number": r.number,
                    "title": r.title,
                    "date": r.date,
                    "status": r.status,
                    "abstract": r.abstract,
                }
                for r in results
            ],
        }

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_rfc_section(
        number: str,
        section: str,
        analyze: bool = True,
    ) -> dict:
        """Extract a specific RFC section with optional normative analysis.

        Returns the section text and, when analyze=True, structured
        normative statements (MUST/SHOULD/MAY) with tag IDs matching
        the bracket-tag format used in Ivy annotations, plus
        cross-references to other RFCs/sections.

        Args:
            number: RFC number (e.g. "4271", "rfc9000").
            section: Section number (e.g. "6.2", "4.1.1").
            analyze: If True (default), include normative statements
                     and cross-references.
        """
        if ctx.rfc_service is None:
            return error_response("RFC service not initialized.")

        try:
            sec = await ctx.rfc_service.get_section(number, section)
        except Exception as exc:
            return error_response(f"Failed to fetch section: {exc}")

        if sec is None:
            return error_response(f"Section {section} not found in RFC {number}.")

        rfc_id = number.lower()
        if not rfc_id.startswith("rfc"):
            rfc_id = f"rfc{rfc_id}"

        result: dict[str, Any] = {
            "status": "ok",
            "rfc": rfc_id,
            "section": sec.number,
            "title": sec.title,
            "text": sec.text,
        }

        if analyze:
            stmts = ctx.rfc_service.extract_normative_statements(sec, rfc=rfc_id)
            refs = ctx.rfc_service.extract_cross_references(sec)

            result["normative_statements"] = [
                {
                    "keyword": s.keyword,
                    "text": s.text,
                    "tag": s.tag,
                }
                for s in stmts
            ]
            result["cross_references"] = [
                {
                    "target_rfc": r.target_rfc,
                    "target_section": r.target_section,
                    "context": r.context,
                }
                for r in refs
            ]

        return result
