"""Tests for the counterexample parser utility.

Covers:
- No counterexample in output -> None
- Counterexample with assertion header, steps, actions, and assignments
- Counterexample header with no steps
- Multiple steps with varying content
"""

import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from ivy_lsp.utils.counterexample_parser import parse_counterexample


class TestParseCounterexample:
    def test_no_counterexample_returns_none(self):
        """Output with no 'Counterexample:' marker returns None."""
        output = "OK\nAll assertions hold.\n"
        assert parse_counterexample(output) is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert parse_counterexample("") is None

    def test_counterexample_with_assertion_and_steps(self):
        """Full counterexample with assertion header, step, action, and assignment."""
        output = (
            "The following assertion at line 42 is not always true:\n"
            "  require conn_state = connected\n"
            "\n"
            "Counterexample:\n"
            "  Step 0:\n"
            "    Action: protocol.send\n"
            "    conn_state = idle\n"
            "    pkt_count = 0\n"
        )
        result = parse_counterexample(output)
        assert result is not None
        assert result["assertion_line"] == 42
        assert "conn_state" in result["assertion"]
        assert len(result["steps"]) == 1

        step = result["steps"][0]
        assert step["step_number"] == 0
        assert step["action"] == "protocol.send"
        assert step["assignments"]["conn_state"] == "idle"
        assert step["assignments"]["pkt_count"] == "0"

    def test_counterexample_header_no_steps(self):
        """Counterexample header present but no steps parsed."""
        output = (
            "Counterexample:\n"
            "  (no trace available)\n"
        )
        result = parse_counterexample(output)
        assert result is not None
        assert result["assertion"] is None
        assert result["assertion_line"] is None
        assert result["steps"] == []

    def test_multiple_steps(self):
        """Counterexample with multiple steps."""
        output = (
            "The following assertion at line 10 is not always true:\n"
            "  assert x > 0\n"
            "\n"
            "Counterexample:\n"
            "  Step 0:\n"
            "    Action: init\n"
            "    x = 0\n"
            "  Step 1:\n"
            "    Action: decrement\n"
            "    x = -1\n"
            "  Step 2:\n"
            "    y = 5\n"
        )
        result = parse_counterexample(output)
        assert result is not None
        assert len(result["steps"]) == 3
        assert result["steps"][0]["action"] == "init"
        assert result["steps"][1]["action"] == "decrement"
        assert result["steps"][1]["assignments"]["x"] == "-1"
        # Step 2 has no action
        assert result["steps"][2]["action"] is None
        assert result["steps"][2]["assignments"]["y"] == "5"

    def test_assertion_without_body(self):
        """Assertion header present but no assertion body line found."""
        output = (
            "The following assertion at line 7 is not always true:\n"
            "\n"
            "Counterexample:\n"
            "  Step 0:\n"
            "    val = false\n"
        )
        result = parse_counterexample(output)
        assert result is not None
        assert result["assertion_line"] == 7
        # No assertion body matched
        assert result["assertion"] is None
        assert len(result["steps"]) == 1

    def test_dotted_variable_names(self):
        """Variables with dotted names are captured correctly."""
        output = (
            "Counterexample:\n"
            "  Step 0:\n"
            "    quic.conn.state = closed\n"
            "    tls.handshake.done = false\n"
        )
        result = parse_counterexample(output)
        assert result is not None
        step = result["steps"][0]
        assert step["assignments"]["quic.conn.state"] == "closed"
        assert step["assignments"]["tls.handshake.done"] == "false"
