"""RFC staleness detection: checks whether manifests are up-to-date."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StalenessReport:
    """Report on the freshness of a requirement manifest."""

    is_stale: bool = False
    reasons: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)
    content_hash_match: Optional[bool] = None
    obsoleted_by: str = ""
    updated_by: str = ""
    errata_ids: List[str] = field(default_factory=list)
    checked_online: bool = False


async def check_staleness(
    manifest_source: str,
    manifest_hash: str,
    rfc_number: str = "",
    check_online: bool = True,
) -> StalenessReport:
    """Check if a manifest is stale relative to its source RFC.

    Args:
        manifest_source: The source URL or path recorded in manifest metadata.
        manifest_hash: The content_hash from manifest metadata.
        rfc_number: The RFC number (e.g. "9000") for metadata lookups.
        check_online: Whether to perform online checks (RFC editor API).

    Returns:
        StalenessReport with staleness status and reasons.
    """
    report = StalenessReport()

    if not manifest_source and not rfc_number:
        report.info.append(
            "No source or RFC number in manifest metadata; " "cannot check staleness."
        )
        return report

    # 1. Content hash comparison (re-fetch source and compare)
    if manifest_source and manifest_hash:
        try:
            from ivy_lsp.rfc.fetcher import fetch_rfc

            result = await fetch_rfc(manifest_source, use_cache=False)
            report.content_hash_match = result.content_hash == manifest_hash
            if not report.content_hash_match:
                report.is_stale = True
                report.reasons.append(
                    "Source content has changed since manifest was generated "
                    f"(expected hash {manifest_hash[:12]}..., "
                    f"got {result.content_hash[:12]}...)"
                )
            else:
                report.info.append("Content hash matches source document.")
        except Exception as exc:
            report.info.append(f"Could not verify content hash: {exc}")

    # 2. RFC editor metadata check (obsolescence, updates, errata)
    if check_online and rfc_number:
        report.checked_online = True
        try:
            rfc_meta = await asyncio.to_thread(_fetch_rfc_metadata, rfc_number)
            if rfc_meta:
                if rfc_meta.get("obsoleted_by"):
                    obs = ", ".join(f"RFC{r}" for r in rfc_meta["obsoleted_by"])
                    report.obsoleted_by = obs
                    report.is_stale = True
                    report.reasons.append(f"RFC {rfc_number} is obsoleted by {obs}")

                if rfc_meta.get("updated_by"):
                    upd = ", ".join(f"RFC{r}" for r in rfc_meta["updated_by"])
                    report.updated_by = upd
                    report.info.append(f"RFC {rfc_number} has been updated by {upd}")

                if rfc_meta.get("errata"):
                    report.errata_ids = [str(e) for e in rfc_meta["errata"]]
                    report.info.append(
                        f"RFC {rfc_number} has {len(report.errata_ids)} "
                        f"errata entries"
                    )
        except Exception as exc:
            report.info.append(f"Could not check RFC editor metadata: {exc}")

    return report


def _fetch_rfc_metadata(rfc_number: str) -> dict:
    """Fetch RFC metadata from the RFC editor JSON API.

    Returns a dict with optional keys: obsoleted_by, updated_by, errata.
    All network errors are caught and return an empty dict.
    """
    import json
    import urllib.request

    url = f"https://www.rfc-editor.org/rfc/rfc{rfc_number}.json"
    result: dict = {}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ivy-lsp/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read(1024 * 1024))

        if isinstance(data, dict):
            obs = data.get("obsoleted_by", [])
            if isinstance(obs, list) and obs:
                result["obsoleted_by"] = obs

            upd = data.get("updated_by", [])
            if isinstance(upd, list) and upd:
                result["updated_by"] = upd

            errata = data.get("errata_url")
            if errata:
                result["errata"] = [errata]

    except Exception as exc:
        logger.debug("RFC metadata fetch failed for %s: %s", rfc_number, exc)

    return result
