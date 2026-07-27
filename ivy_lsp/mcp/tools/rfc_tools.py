"""RFC lookup, search, and section analysis MCP tools."""

from __future__ import annotations

import logging
from typing import Any, Literal

from ivy_lsp.mcp.tools import error_response, safe_tool

logger = logging.getLogger(__name__)


async def _rfc_get_impl(number: str, format: str, ctx: Any) -> dict:
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
        result["note"] = (
            "Metadata extraction from RFC headers is not yet implemented. "
            "Use format='sections' for a table of contents."
        )
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


async def _rfc_search_impl(query: str, limit: int, ctx: Any) -> dict:
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


async def _rfc_section_impl(number: str, section: str, analyze: bool, ctx: Any) -> dict:
    if ctx.rfc_service is None:
        return error_response("RFC service not initialized.")

    try:
        doc = await ctx.rfc_service.get_rfc(number, format="full")
    except Exception as exc:
        return error_response(f"Failed to fetch RFC {number}: {exc}")

    sec = next((s for s in doc.sections if s.number == section), None)
    if sec is None:
        return error_response(f"Section {section} not found in RFC {number}.")

    result: dict[str, Any] = {
        "status": "ok",
        "rfc": doc.number,
        "section": sec.number,
        "title": sec.title,
        "text": sec.text,
    }

    if analyze:
        stmts = ctx.rfc_service.extract_normative_statements(sec, rfc=doc.number)
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


def register_rfc_tools(mcp: Any, ctx: Any) -> None:
    """Register RFC MCP tools."""

    @mcp.tool()
    @safe_tool(ctx=ctx)
    async def ivy_rfc(
        mode: Literal["get", "search", "section"] = "get",
        number: str | None = None,
        query: str | None = None,
        format: str = "full",
        section: str | None = None,
        analyze: bool = True,
        limit: int = 10,
    ) -> dict:
        """Retrieves, searches, and extracts RFC documents.

        Modes:
        - get: fetch full RFC by number → {number, title, sections: [{number, title, text}]}
        - search: keyword search via IETF Datatracker → {results: [{number, title, date, status, abstract}], count}
        - section: extract one section with optional normative analysis → {rfc, section, title, text, normative_statements?: [{keyword, text, tag}]}

        Use search to find RFC numbers, then get or section for content.

        Args:
            mode: Operation mode — "get", "search", or "section".
            number: RFC number (e.g. "4271", "rfc9000"). Required for get and section.
            query: Search terms (e.g. "BGP path attributes"). Required for search.
            format: For get mode — "full", "metadata", or "sections".
            section: Section number (e.g. "6.2"). Required for section mode.
            analyze: For section mode — include normative statements and cross-references.
            limit: For search mode — maximum number of results.
        """
        if mode == "get":
            if number is None:
                return error_response("'number' is required for mode='get'.")
            return await _rfc_get_impl(number, format, ctx)

        if mode == "search":
            if query is None:
                return error_response("'query' is required for mode='search'.")
            return await _rfc_search_impl(query, limit, ctx)

        if mode == "section":
            if number is None:
                return error_response("'number' is required for mode='section'.")
            if section is None:
                return error_response("'section' is required for mode='section'.")
            return await _rfc_section_impl(number, section, analyze, ctx)

        return error_response(f"Unknown mode '{mode}'. Valid: get, search, section.")
