"""Backward-compat shim — re-exports all names including private."""

from ivy_lsp.core.analysis import requirement_graph as _real  # noqa: E402

globals().update({k: v for k, v in _real.__dict__.items() if not k.startswith("__")})
del _real
