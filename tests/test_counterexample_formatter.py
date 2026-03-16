# tests/test_counterexample_formatter.py
"""Tests for counterexample formatting."""
from ivy_lsp.utils.counterexample_formatter import format_counterexample


def test_format_empty_counterexample():
    cex = {"assertion": None, "assertion_line": None, "steps": []}
    result = format_counterexample(cex)
    assert "No assertion" in result


def test_format_single_step():
    cex = {
        "assertion": "require conn_seen(C)",
        "assertion_line": 42,
        "steps": [
            {
                "step_number": 1,
                "action": "quic_connection.open",
                "assignments": {"conn_seen": "false", "cid": "0x1234"},
            }
        ],
    }
    result = format_counterexample(cex)
    assert "Line 42" in result
    assert "require conn_seen(C)" in result
    assert "Step 1" in result
    assert "quic_connection.open" in result
    assert "conn_seen = false" in result


def test_format_multi_step_trace():
    cex = {
        "assertion": "ensure stream_data_sent(S)",
        "assertion_line": 108,
        "steps": [
            {
                "step_number": 1,
                "action": "quic_stream.open",
                "assignments": {"stream_id": "4", "stream_state": "idle"},
            },
            {
                "step_number": 2,
                "action": "quic_stream.send",
                "assignments": {"stream_id": "4", "stream_state": "open", "bytes_sent": "0"},
            },
        ],
    }
    result = format_counterexample(cex)
    assert "Step 1" in result
    assert "Step 2" in result
    # Should show state changes between steps
    assert "stream_state" in result


def test_format_none_returns_empty():
    assert format_counterexample(None) == ""
