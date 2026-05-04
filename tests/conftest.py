"""Shared fixtures for Ivy LSP tests."""

import os
import subprocess
from pathlib import Path

import pytest

from ivy_lsp.infra.config import reset_config


@pytest.fixture(autouse=True)
def _reset_server_config():
    """Ensure the ServerConfig singleton is fresh for every test.

    Tests that set ``IVY_LSP_*`` env vars via ``monkeypatch`` or
    ``patch.dict`` would otherwise leak cached values to subsequent tests.
    """
    reset_config()
    yield
    reset_config()


@pytest.fixture(autouse=True)
def _raw_json_for_legacy_tests(request):
    """Enable raw JSON output for tests that parse tool results as JSON.

    The markdown formatter layer (ivy_lsp.mcp.tools.formatters) converts tool
    output from JSON to markdown.  Legacy tests that call ``json.loads``
    on tool results need the raw JSON.  Set ``IVY_LSP_RAW_JSON=1`` for
    every test **except** those in ``test_formatters.py`` (which test the
    formatter layer itself).
    """
    module_name = request.module.__name__
    if "test_formatters" in module_name:
        # Formatter tests want markdown output — do NOT set the bypass flag
        os.environ.pop("IVY_LSP_RAW_JSON", None)
        yield
    else:
        os.environ["IVY_LSP_RAW_JSON"] = "1"
        yield
        os.environ.pop("IVY_LSP_RAW_JSON", None)


# Try to resolve PROTOCOL_TESTING_DIR from the ivy package (if installed),
# otherwise fall back to a sibling layout (panther_ivy checkout).
try:
    import ivy

    # Guard against namespace-package `ivy` whose ``__file__`` is None
    # (the import succeeds but the package has no concrete file path).
    # Treat that as the no-installed-package case so the sibling-layout
    # fallback below kicks in.
    _ivy_file = getattr(ivy, "__file__", None)
    _IVY_PKG_DIR = Path(_ivy_file).resolve().parent if _ivy_file else None
except ImportError:
    _IVY_PKG_DIR = None

if _IVY_PKG_DIR is not None:
    PROTOCOL_TESTING_DIR: Path | None = _IVY_PKG_DIR.parent / "protocol-testing"
else:
    # Fallback: when running from a panther_ivy checkout
    PROTOCOL_TESTING_DIR = Path(__file__).resolve().parent.parent / "protocol-testing"

if not PROTOCOL_TESTING_DIR.exists():
    PROTOCOL_TESTING_DIR = None

QUIC_STACK_DIR = (
    PROTOCOL_TESTING_DIR / "quic" / "quic_stack"
    if PROTOCOL_TESTING_DIR is not None
    else Path(__file__).resolve().parent.parent
    / "protocol-testing"
    / "quic"
    / "quic_stack"
)


# ---------------------------------------------------------------------------
# Minimal Ivy source fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ivy_source_mixin():
    """Ivy source with before/after mixin declarations."""
    return """\
#lang ivy1.7

type t

object foo = {
    action step(x:t)
}

before foo.step {
    require x ~= x;
}

after foo.step {
    ensure true;
}
"""


# ---------------------------------------------------------------------------
# Real file path fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def quic_types_path():
    """Path to the real quic_types.ivy file."""
    path = QUIC_STACK_DIR / "quic_types.ivy"
    if not path.exists():
        pytest.skip(f"quic_types.ivy not found at {path}")
    return path


@pytest.fixture
def quic_types_source(quic_types_path):
    """Source content of quic_types.ivy."""
    return quic_types_path.read_text()


@pytest.fixture
def quic_stack_ivy_files():
    """List of all .ivy files in the QUIC stack directory."""
    if not QUIC_STACK_DIR.exists():
        pytest.skip(f"QUIC stack directory not found at {QUIC_STACK_DIR}")
    return sorted(QUIC_STACK_DIR.glob("*.ivy"))


# ---------------------------------------------------------------------------
# MiniP workspace fixtures (self-contained test repo)
# ---------------------------------------------------------------------------

MINIP_REPO_ROOT = Path(__file__).resolve().parent / "resources" / "repos" / "minip"
MINIP_DIR = MINIP_REPO_ROOT / "protocol-testing" / "minip"
MINIP_STACK_DIR = MINIP_DIR / "minip_stack"
MINIP_TESTS_DIR = MINIP_DIR / "minip_tests"


@pytest.fixture
def minip_workspace_dir():
    """Path to the minip workspace root (protocol-testing/minip/)."""
    if not MINIP_DIR.exists():
        pytest.skip(f"minip workspace not found at {MINIP_DIR}")
    return MINIP_DIR


@pytest.fixture
def minip_stack_dir():
    """Path to the minip_stack/ directory."""
    if not MINIP_STACK_DIR.exists():
        pytest.skip(f"minip stack not found at {MINIP_STACK_DIR}")
    return MINIP_STACK_DIR


@pytest.fixture
def minip_stack_ivy_files():
    """List of all .ivy files in the minip stack directory."""
    if not MINIP_STACK_DIR.exists():
        pytest.skip(f"minip stack not found at {MINIP_STACK_DIR}")
    return sorted(MINIP_STACK_DIR.glob("*.ivy"))


@pytest.fixture
def minip_all_ivy_files():
    """List of ALL .ivy files across all minip subdirectories."""
    if not MINIP_DIR.exists():
        pytest.skip(f"minip workspace not found at {MINIP_DIR}")
    return sorted(MINIP_DIR.rglob("*.ivy"))


@pytest.fixture
def minip_test_ivy_files():
    """List of all .ivy test spec files across minip_tests/."""
    if not MINIP_TESTS_DIR.exists():
        pytest.skip(f"minip tests not found at {MINIP_TESTS_DIR}")
    return sorted(MINIP_TESTS_DIR.rglob("*.ivy"))


@pytest.fixture
def minip_types_path():
    """Path to ping_types.ivy (richest type file in minip)."""
    path = MINIP_STACK_DIR / "ping_types.ivy"
    if not path.exists():
        pytest.skip(f"ping_types.ivy not found at {path}")
    return path


@pytest.fixture
def minip_types_source(minip_types_path):
    """Source content of ping_types.ivy."""
    return minip_types_path.read_text()


@pytest.fixture(scope="module")
def minip_indexer():
    """Pre-built WorkspaceIndexer for the full minip workspace (module-scoped).

    Uses MINIP_DIR as workspace root with staging so cross-directory
    includes resolve via flat symlinks.

    Requires the ``ivy`` package for tokenization — skips when unavailable.
    """
    if not MINIP_DIR.exists():
        pytest.skip(f"minip workspace not found at {MINIP_DIR}")
    try:
        import ivy  # noqa: F401
    except ImportError:
        pytest.skip("ivy package not installed (required for indexer)")
    from ivy_lsp.core.indexer.include_resolver import IncludeResolver
    from ivy_lsp.core.indexer.workspace_indexer import WorkspaceIndexer
    from ivy_lsp.core.parsing.parser_session import IvyParserWrapper

    parser = IvyParserWrapper()
    resolver = IncludeResolver(str(MINIP_DIR))
    resolver.create_staging_directory()
    indexer = WorkspaceIndexer(str(MINIP_DIR), parser, resolver)
    indexer.index_workspace()
    yield indexer
    resolver.cleanup_staging()


# ---------------------------------------------------------------------------
# Multi-file workspace fixtures (tmp_path-based)
# ---------------------------------------------------------------------------


@pytest.fixture
def annotated_workspace(tmp_path):
    """Workspace with RFC bracket-tag annotations and a requirements manifest.

    Creates:
      test_requirements.yaml  — manifest with rfc9000:4.1 (MUST) and rfc9000:8.1 (SHOULD)
      types.ivy               — type cid
      monitor.ivy             — annotated require/ensure with bracket tags
    """
    manifest = tmp_path / "test_requirements.yaml"
    manifest.write_text(
        "rfc: RFC9000\n"
        "requirements:\n"
        "  rfc9000:4.1:\n"
        "    text: Sender MUST open connection before sending.\n"
        "    section: '4.1'\n"
        "    level: MUST\n"
        "    layer: transport\n"
        "    testable: true\n"
        "  rfc9000:8.1:\n"
        "    text: Receiver SHOULD validate address.\n"
        "    section: '8.1'\n"
        "    level: SHOULD\n"
        "    layer: transport\n"
        "    testable: true\n"
    )
    types_file = tmp_path / "types.ivy"
    types_file.write_text("#lang ivy1.7\n\ntype cid\n")
    monitor_file = tmp_path / "monitor.ivy"
    monitor_file.write_text(
        "#lang ivy1.7\n"
        "\n"
        "include types\n"
        "\n"
        "action send(src:cid, dst:cid)\n"
        "\n"
        "before send {\n"
        "    require src ~= dst; # [rfc9000:4.1]\n"
        "}\n"
        "\n"
        "after send {\n"
        "    ensure true;\n"
        "}\n"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Real protocol-testing dir fixtures (propagation integration tests)
# ---------------------------------------------------------------------------

# Path to the REAL MiniP protocol files (not the test-resources copy).
# The propagation tools need actual ser/deser files with C++ impl blocks.
_REAL_PROTOCOL_TESTING = (
    PROTOCOL_TESTING_DIR
    if PROTOCOL_TESTING_DIR is not None
    else Path(__file__).resolve().parent.parent / "protocol-testing"
)
_REAL_MINIP_DIR = _REAL_PROTOCOL_TESTING / "minip"


@pytest.fixture(scope="session")
def minip_protocol_dir():
    """Path to the REAL MiniP protocol files for propagation tool tests."""
    env_dir = os.environ.get("PANTHER_IVY_PROTOCOL_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    if _REAL_MINIP_DIR.exists():
        return str(_REAL_MINIP_DIR)
    pytest.skip(f"Real MiniP protocol dir not found at {_REAL_MINIP_DIR}")


@pytest.fixture
def minip_worktree(tmp_path):
    """Create an isolated Git worktree copy of MiniP for destructive tests.

    Yields the path to the worktree's minip/ directory.
    Cleans up the worktree after the test.
    """
    repo_root = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
    worktree_path = str(tmp_path / "propagation-test")

    subprocess.run(
        ["git", "worktree", "add", "--detach", worktree_path, "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    try:
        minip_dir = os.path.join(
            worktree_path,
            "panther",
            "plugins",
            "services",
            "testers",
            "panther_ivy",
            "protocol-testing",
            "minip",
        )
        if not os.path.isdir(minip_dir):
            pytest.skip(f"MiniP not found in worktree: {minip_dir}")
        yield minip_dir
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=repo_root,
            capture_output=True,
        )
