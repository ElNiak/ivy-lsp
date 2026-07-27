"""Shared navigation handler infrastructure."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lsprotocol import types as lsp

from ivy_lsp.infra.utils import uri_to_path
from ivy_lsp.infra.utils.position_utils import make_range

logger = logging.getLogger(__name__)


def scoped_lookup_to_location(sl) -> lsp.Location:
    """Convert a ``ScopedLookupResult`` to an LSP ``Location``."""
    uri = Path(sl.filepath).as_uri() if sl.filepath else ""
    return lsp.Location(uri=uri, range=make_range(*sl.range))


def symbol_to_location(sym) -> lsp.Location:
    """Convert an ``IvySymbol`` to an LSP ``Location``."""
    uri = Path(sym.file_path).as_uri() if sym.file_path else ""
    return lsp.Location(uri=uri, range=make_range(*sym.range))


@dataclass(frozen=True)
class NavigationContext:
    """Shared context prepared for all navigation handlers."""

    uri: str
    doc: Any
    lines: list[str]
    filepath: str
    position: Any  # lsp.Position
    model: Any  # server.semantic_model (may be None)
    indexer: Any  # server.indexer (guaranteed non-None)
    server: Any  # full server reference for edge cases


async def run_navigation_handler(
    params,
    server,
    compute_fn: Callable[[NavigationContext], Any],
    *,
    trace_method: str | None = None,
    track_active_uri: bool = False,
):
    """Common async wrapper: setup -> executor dispatch -> error handling.

    Returns None if indexer is unavailable or compute_fn raises.
    """
    uri = params.text_document.uri
    if track_active_uri:
        server._last_active_uri = uri
    doc = server.workspace.get_text_document(uri)
    if server.indexer is None:
        return None
    lines = doc.source.split("\n") if doc.source else []
    filepath = uri_to_path(uri)
    ctx = NavigationContext(
        uri=uri,
        doc=doc,
        lines=lines,
        filepath=filepath,
        position=params.position,
        model=server.semantic_model,
        indexer=server.indexer,
        server=server,
    )
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, compute_fn, ctx)
    except Exception:
        logger.warning(
            "Error in %s for %s",
            trace_method or "navigation",
            filepath,
            exc_info=True,
        )
        return None
