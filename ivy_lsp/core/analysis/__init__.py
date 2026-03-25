"""Requirements analysis layer for Ivy LSP.

Provides requirement extraction from Ivy AST bodies, formula state-variable
analysis, a typed requirement graph with cross-file propagation, and a
regex-based light-mode fallback for environments without Z3.
"""
