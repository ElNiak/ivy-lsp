"""Shared fixtures for Ivy LSP tests."""

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
def ivy_source_minimal():
    """Minimal valid Ivy source with a single type declaration."""
    return "#lang ivy1.7\n\ntype cid\n"


@pytest.fixture
def ivy_source_object():
    """Ivy source with a nested object containing type and individuals."""
    return """\
#lang ivy1.7

object bit = {
    type this
    individual zero:bit
    individual one:bit
    definition zero = 0
    definition one = 1
}
"""


@pytest.fixture
def ivy_source_complex():
    """Complex Ivy source with multiple declaration types."""
    return """\
#lang ivy1.7

type cid
type pkt_num
alias aid = cid

object bit = {
    type this
    individual zero:bit
    individual one:bit
}

object role = {
    type this = {client, server}
}

action send(src:cid, dst:cid, pkt:pkt_num) = {
    require src ~= dst;
}

relation connected(X:cid, Y:cid)
"""


@pytest.fixture
def ivy_source_syntax_error():
    """Ivy source with a syntax error."""
    return """\
#lang ivy1.7

type cid
object broken = {
    type this
    this is not valid ivy syntax !!!
}
type pkt_num
"""


@pytest.fixture
def ivy_source_module():
    """Ivy source with a module declaration."""
    return """\
#lang ivy1.7

module counter(t) = {
    individual val : t

    action up = {
        val := val + 1;
    }

    action down = {
        val := val - 1;
    }
}
"""


@pytest.fixture
def ivy_source_include():
    """Ivy source with an include directive."""
    return """\
#lang ivy1.7

include quic_types

type my_type
"""


@pytest.fixture
def ivy_source_isolate():
    """Ivy source with an isolate declaration."""
    return """\
#lang ivy1.7

type node

object protocol = {
    action step(n:node)
    action init(n:node)
}

isolate iso_protocol = protocol
"""


@pytest.fixture
def ivy_source_property():
    """Ivy source with property and axiom declarations."""
    return """\
#lang ivy1.7

type t

relation r(X:t, Y:t)

axiom [symmetry] r(X,Y) -> r(Y,X)
property [reflexivity] r(X,X)
"""


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


@pytest.fixture
def ivy_source_instance():
    """Ivy source with an instance declaration."""
    return """\
#lang ivy1.7

module unbounded_sequence = {
    type this
    action next(x:this) returns (y:this)
}

instance idx : unbounded_sequence
"""


@pytest.fixture
def ivy_source_enum():
    """Ivy source with enum type."""
    return """\
#lang ivy1.7

type stream_kind = {unidir, bidir}
"""


@pytest.fixture
def ivy_source_variant():
    """Ivy source with an enumerated type (packet_type)."""
    return """\
#lang ivy1.7

type packet_type = {initial, handshake, one_rtt}
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
def quic_frame_path():
    """Path to the real quic_frame.ivy file."""
    path = QUIC_STACK_DIR / "quic_frame.ivy"
    if not path.exists():
        pytest.skip(f"quic_frame.ivy not found at {path}")
    return path


@pytest.fixture
def quic_frame_source(quic_frame_path):
    """Source content of quic_frame.ivy."""
    return quic_frame_path.read_text()


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
def multi_file_workspace(tmp_path):
    """Two-file workspace with include dependency and shared symbols.

    Creates:
      types.ivy  — declares ``type cid`` and ``type pkt_num``
      conn.ivy   — includes types, declares ``action send(src:cid, dst:cid)``
                   and uses ``cid`` in a ``relation connected(X:cid, Y:cid)``
    """
    types_file = tmp_path / "types.ivy"
    types_file.write_text("#lang ivy1.7\n" "\n" "type cid\n" "type pkt_num\n")
    conn_file = tmp_path / "conn.ivy"
    conn_file.write_text(
        "#lang ivy1.7\n"
        "\n"
        "include types\n"
        "\n"
        "action send(src:cid, dst:cid) = {\n"
        "    require src ~= dst;\n"
        "}\n"
        "\n"
        "relation connected(X:cid, Y:cid)\n"
    )
    return tmp_path


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


@pytest.fixture
def syntax_error_workspace(tmp_path):
    """Workspace with files containing deliberate structural errors.

    Creates:
      no_header.ivy     — missing #lang header
      bad_braces.ivy    — unmatched braces
      bad_include.ivy   — unresolvable include
    """
    (tmp_path / "no_header.ivy").write_text("type cid\n")
    (tmp_path / "bad_braces.ivy").write_text("#lang ivy1.7\n\ntype a = { b\n")
    (tmp_path / "bad_include.ivy").write_text(
        "#lang ivy1.7\n\ninclude nonexistent_module\n\ntype x\n"
    )
    return tmp_path


@pytest.fixture
def large_workspace_source():
    """Ivy source with 160 symbol declarations for cap testing."""
    lines = ["#lang ivy1.7\n"]
    for i in range(80):
        lines.append(f"type sym_type_{i}")
    for i in range(80):
        lines.append(f"action sym_action_{i}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def ivy_source_test_file():
    """Ivy source simulating a test file with exports but no _finalize."""
    return """\
#lang ivy1.7

type cid
action send(src:cid, dst:cid)
export send

before send {
    require src ~= dst;
}
"""


@pytest.fixture
def ivy_source_untagged_assertion():
    """Ivy source with assertions that lack bracket-tag annotations."""
    return """\
#lang ivy1.7

type cid
action send(src:cid, dst:cid)

before send {
    require src ~= dst;
    ensure true;
}
"""
