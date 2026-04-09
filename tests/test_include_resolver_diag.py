"""Tests for cross-layer include diagnostics (D2 scaffold)."""

from ivy_lsp.core.structural_lint import check_unresolved_includes_raw


def test_cross_layer_include_fires_unresolved():
    """When an include resolves to a different layer, it should fire a diagnostic.

    Full D2 (ivy.include.crossLayer) requires resolver-level integration
    to distinguish same-layer resolution from cross-layer resolution.
    This test verifies the unresolved-include or near-miss diagnostic
    fires as a baseline.
    """
    source = "#lang ivy1.7\ninclude quic_time\n"

    def resolver(name: str, from_file: str) -> None:
        return None  # not found in own layer

    issues = check_unresolved_includes_raw(
        source,
        "/fake/apt/test.ivy",
        resolve_callback=resolver,
        basename_map={"quic_time": ["/fake/quic_standard/quic_time.ivy"]},
    )
    # Near-miss won't fire (exact name exists but in wrong layer),
    # so unresolved-include should fire
    assert len(issues) >= 1
