"""Tests for IncludeResolver standard-library directory resolution.

Covers the workspace-first resolution chain landed in PR4 of the
harness audit Tier-L bundle:

  1. ``self._ivy_include_path`` — explicit override
  2. Workspace walk-up — preferred for development inside a panther_ivy
     checkout (worktree edits to ``ivy/include/*.ivy`` win over any
     installed ivy package)
  3. ``import ivy`` fallback — for runs outside a panther_ivy checkout

Plus the drift detector that warns when (2) and (3) both resolve to
different paths, and the namespace-package crash guard.

Regression for the audit's "wrong number of arguments to module
network_implementation" issue, which was caused by the resolver
picking up a stale 5-arg copy of network_implementation.ivy from a
system install while the worktree had a 6-arg version.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ivy_lsp.core.indexer.include_resolver import IncludeResolver

pytestmark = pytest.mark.unit

# Minimal panther_ivy stdlib fixture — contains the canonical sentinel
# (``network_implementation.ivy``) plus a couple of other stdlib-shaped
# files. Content is deliberately empty/trivial; tests only check
# resolution paths, not parse semantics.
_SENTINEL = "network_implementation.ivy"


def _make_panther_ivy_layout(root: Path, version: str = "1.7") -> Path:
    """Build a fake panther_ivy checkout under *root*.

    Returns the protocol-testing/<protocol> directory the tests use as
    workspace_root, so the resolver can walk up to find the stdlib.
    """
    inc = root / "ivy" / "include" / version
    inc.mkdir(parents=True)
    (inc / _SENTINEL).write_text("# stub network_implementation\n")
    (inc / "tcp_impl.ivy").write_text("# stub tcp_impl\n")

    proto_dir = root / "protocol-testing" / "myprotocol"
    proto_dir.mkdir(parents=True)
    return proto_dir


def test_walkup_finds_workspace_stdlib(tmp_path: Path) -> None:
    """Resolver walks up from workspace_root and finds the panther_ivy stdlib."""
    proto_dir = _make_panther_ivy_layout(tmp_path)

    resolver = IncludeResolver(str(proto_dir))
    std = resolver._get_std_include_dir()

    expected = tmp_path / "ivy" / "include" / "1.7"
    assert std is not None
    assert Path(std).resolve() == expected.resolve()


def test_walkup_prefers_1_7_over_higher_versions(tmp_path: Path) -> None:
    """When multiple version dirs exist, the resolver prefers 1.7."""
    _make_panther_ivy_layout(tmp_path, version="1.7")
    # Add a 1.8 dir with the sentinel too
    inc_18 = tmp_path / "ivy" / "include" / "1.8"
    inc_18.mkdir()
    (inc_18 / _SENTINEL).write_text("# stub\n")

    proto_dir = tmp_path / "protocol-testing" / "myprotocol"
    resolver = IncludeResolver(str(proto_dir))
    std = resolver._get_std_include_dir()

    # 1.7 wins because it matches `#lang ivy1.7` discipline.
    assert std is not None
    assert Path(std).name == "1.7"


def test_walkup_falls_back_to_highest_version_when_no_1_7(tmp_path: Path) -> None:
    """When 1.7 is absent, the resolver picks the highest version with the sentinel."""
    inc_18 = tmp_path / "ivy" / "include" / "1.8"
    inc_18.mkdir(parents=True)
    (inc_18 / _SENTINEL).write_text("# stub\n")
    inc_17_5 = tmp_path / "ivy" / "include" / "1.7.5"
    inc_17_5.mkdir()
    (inc_17_5 / _SENTINEL).write_text("# stub\n")

    proto_dir = tmp_path / "protocol-testing" / "myprotocol"
    proto_dir.mkdir(parents=True)
    resolver = IncludeResolver(str(proto_dir))
    std = resolver._get_std_include_dir()

    assert std is not None
    assert Path(std).name == "1.8"


def test_walkup_skips_directories_without_sentinel(tmp_path: Path) -> None:
    """An ivy/include/<version>/ dir without the sentinel is rejected.

    Prevents false positives where an unrelated directory tree happens
    to contain an ``ivy/include/<version>/`` layout (e.g., a vendor
    bundle of unrelated specs).
    """
    inc = tmp_path / "ivy" / "include" / "1.7"
    inc.mkdir(parents=True)
    # NO sentinel file — the resolver should reject this dir
    (inc / "some_other_module.ivy").write_text("# not the sentinel\n")

    proto_dir = tmp_path / "protocol-testing" / "myprotocol"
    proto_dir.mkdir(parents=True)
    resolver = IncludeResolver(str(proto_dir))
    std = resolver._discover_workspace_stdlib()

    # Sentinel-free dir is not accepted as panther_ivy stdlib
    assert std is None


def test_explicit_path_overrides_walkup(tmp_path: Path) -> None:
    """Explicit ivy_include_path beats workspace walk-up."""
    proto_dir = _make_panther_ivy_layout(tmp_path)

    other_inc = tmp_path / "alt-stdlib"
    other_inc.mkdir()
    resolver = IncludeResolver(str(proto_dir), ivy_include_path=str(other_inc))
    std = resolver._get_std_include_dir()

    assert std == str(other_inc)


def test_no_workspace_stdlib_falls_through_to_import_ivy(tmp_path: Path) -> None:
    """When walk-up finds nothing, the resolver still uses import-ivy fallback.

    This ensures the change preserves the previous behaviour for
    callers running outside a panther_ivy checkout (e.g., system tools
    that just want the installed stdlib).
    """
    # workspace_root is a plain dir with no panther_ivy ancestor
    resolver = IncludeResolver(str(tmp_path))
    ws_std = resolver._discover_workspace_stdlib()
    assert ws_std is None

    # The full chain still returns whatever import-ivy resolves to (or
    # None if ivy isn't a concrete-file package). This test does NOT
    # assert a specific path because the fallback depends on the test
    # environment, but it must not crash.
    _ = resolver._get_std_include_dir()  # no exception


def test_namespace_package_ivy_does_not_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against ``ivy.__file__`` being None (namespace package).

    Mirrors the conftest.py guard from PR2's PR2.A commit. The resolver
    should treat the namespace-package case as "no fallback stdlib"
    rather than propagating ``TypeError: realpath(None)``.
    """
    import sys
    import types

    fake_ivy = types.ModuleType("ivy")
    fake_ivy.__file__ = None  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "ivy", fake_ivy)

    resolver = IncludeResolver(str(tmp_path))
    # Should return None gracefully (no workspace stdlib in tmp_path,
    # and the import-ivy fallback degrades to None on __file__=None).
    std = resolver._get_std_include_dir()
    assert std is None


def test_drift_warning_emitted_on_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When workspace stdlib AND import-ivy disagree, log [ivy-stale-stdlib]."""
    import sys
    import types

    # Fake an installed ivy package with its own include/1.7/ tree.
    stale_ivy_pkg_dir = tmp_path / "stale-install" / "ivy"
    stale_ivy_pkg_dir.mkdir(parents=True)
    (stale_ivy_pkg_dir / "__init__.py").write_text("")
    stale_inc = stale_ivy_pkg_dir / "include" / "1.7"
    stale_inc.mkdir(parents=True)
    (stale_inc / _SENTINEL).write_text("# stale\n")
    fake_ivy = types.ModuleType("ivy")
    fake_ivy.__file__ = str(stale_ivy_pkg_dir / "__init__.py")
    monkeypatch.setitem(sys.modules, "ivy", fake_ivy)

    # Set up a workspace whose ancestor has a DIFFERENT stdlib path.
    proto_dir = _make_panther_ivy_layout(tmp_path / "worktree")
    expected_ws_std = (tmp_path / "worktree" / "ivy" / "include" / "1.7").resolve()

    resolver = IncludeResolver(str(proto_dir))
    with caplog.at_level(logging.WARNING):
        std = resolver._get_std_include_dir()

    # Workspace path wins.
    assert std is not None
    assert Path(std).resolve() == expected_ws_std

    # Drift warning was emitted.
    drift_lines = [
        r.message for r in caplog.records if "[ivy-stale-stdlib]" in r.message
    ]
    assert drift_lines, "expected [ivy-stale-stdlib] drift warning to be logged"
    msg = drift_lines[0]
    assert str(expected_ws_std) in msg
    assert "stale install" in msg


def test_drift_warning_silent_when_paths_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No drift warning when workspace stdlib equals import-ivy stdlib.

    Regression: the warning must not be a constant noise floor for the
    common case where the installed ivy IS the worktree (e.g.,
    ``pip install -e panther_ivy``).
    """
    import sys
    import types

    proto_dir = _make_panther_ivy_layout(tmp_path)
    ws_std_dir = tmp_path / "ivy" / "include" / "1.7"

    # Fake an installed ivy package pointing at the SAME stdlib dir.
    # (Simulates `pip install -e` resolving ivy to the worktree.)
    fake_ivy = types.ModuleType("ivy")
    fake_ivy.__file__ = str(tmp_path / "ivy" / "__init__.py")
    (tmp_path / "ivy" / "__init__.py").write_text("")
    monkeypatch.setitem(sys.modules, "ivy", fake_ivy)

    resolver = IncludeResolver(str(proto_dir))
    with caplog.at_level(logging.WARNING):
        std = resolver._get_std_include_dir()

    assert std is not None
    assert Path(std).resolve() == ws_std_dir.resolve()
    drift_lines = [
        r.message for r in caplog.records if "[ivy-stale-stdlib]" in r.message
    ]
    assert not drift_lines, f"unexpected drift warning when paths agree: {drift_lines}"
