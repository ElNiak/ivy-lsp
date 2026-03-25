"""Utility functions for the Ivy LSP server."""

from urllib.parse import unquote, urlparse


def uri_to_path(uri: str) -> str:
    """Convert a ``file://`` URI to a local filesystem path.

    Handles percent-encoded characters (e.g. spaces as ``%20``) and
    the optional authority component.  Falls back to simple prefix
    stripping for non-``file`` schemes or malformed URIs.
    """
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        return unquote(parsed.path)
    # Fallback for unexpected schemes — best-effort.
    return uri.replace("file://", "")
