"""RFC fetching, parsing, analysis, caching, and search."""

from ivy_lsp.core.rfc.analyzer import RfcAnalyzer
from ivy_lsp.core.rfc.cache import RfcCache
from ivy_lsp.core.rfc.fetcher import FetchError, FetchResult, fetch_rfc
from ivy_lsp.core.rfc.parser import ParsedRfc, RfcSection, parse_rfc_text
from ivy_lsp.core.rfc.search import DataTrackerClient
from ivy_lsp.core.rfc.service import RfcService
from ivy_lsp.core.rfc.types import (
    CrossReference,
    NormativeStatement,
    RfcDocument,
    RfcMetadata,
    RfcSearchResult,
)

__all__ = [
    "CrossReference",
    "DataTrackerClient",
    "FetchError",
    "FetchResult",
    "NormativeStatement",
    "ParsedRfc",
    "RfcAnalyzer",
    "RfcCache",
    "RfcDocument",
    "RfcMetadata",
    "RfcSearchResult",
    "RfcSection",
    "RfcService",
    "fetch_rfc",
    "parse_rfc_text",
]
