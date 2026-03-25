"""Shared fixtures for Ivy LSP tests."""

import os
from pathlib import Path

import pytest

from ivy_lsp.config import reset_config


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

    The markdown formatter layer (ivy_lsp.tools.formatters) converts tool
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


# Try to resolve QUIC_STACK_DIR from the ivy package (if installed),
# otherwise fall back to a sibling layout (panther_ivy checkout).
try:
    import ivy

    _IVY_PKG_DIR = Path(ivy.__file__).resolve().parent
except ImportError:
    _IVY_PKG_DIR = None

if _IVY_PKG_DIR is not None:
    QUIC_STACK_DIR = _IVY_PKG_DIR.parent / "protocol-testing" / "quic" / "quic_stack"
else:
    # Fallback: when running from a panther_ivy checkout
    QUIC_STACK_DIR = (
        Path(__file__).resolve().parent.parent
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
