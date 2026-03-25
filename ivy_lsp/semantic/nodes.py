"""Backward-compat shim — re-exports all names including private."""

from ivy_lsp.core.semantic import nodes as _real  # noqa: E402

globals().update({k: v for k, v in _real.__dict__.items() if not k.startswith("__")})
del _real
