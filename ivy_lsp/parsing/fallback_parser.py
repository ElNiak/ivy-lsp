"""Backward-compat shim — re-exports all names including private."""

from ivy_lsp.core.parsing import fallback_parser as _real  # noqa: E402

globals().update({k: v for k, v in _real.__dict__.items() if not k.startswith("__")})
del _real
