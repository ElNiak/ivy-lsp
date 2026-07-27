"""Tests for command operation tracking instrumentation."""

from ivy_lsp.lsp.ui.status import ServerStateTracker


class TestCommandInstrumentation:
    """Verify that the tracking helpers work correctly with the tracker."""

    def test_tracker_records_verify_pattern(self):
        """Simulate the verify command tracking pattern."""
        tracker = ServerStateTracker()
        op_id = tracker.operation_tracker.record_start("verify", "test.ivy")
        tracker.operation_tracker.record_end(
            op_id, success=True, message="OK", duration=2.0
        )
        history = tracker.operation_tracker.get_history()
        assert len(history) == 1
        assert history[0].type == "verify"

    def test_tracker_records_compile_pattern(self):
        tracker = ServerStateTracker()
        op_id = tracker.operation_tracker.record_start("compile", "test.ivy")
        tracker.operation_tracker.record_end(
            op_id, success=False, message="Exit code 1", duration=5.0
        )
        history = tracker.operation_tracker.get_history()
        assert len(history) == 1
        assert history[0].success is False

    def test_tracker_records_show_model_pattern(self):
        tracker = ServerStateTracker()
        op_id = tracker.operation_tracker.record_start("showModel", "test.ivy")
        tracker.operation_tracker.record_end(
            op_id, success=True, message="OK", duration=0.5
        )
        history = tracker.operation_tracker.get_history()
        assert history[0].type == "showModel"

    def test_tracker_records_compile_test_pattern(self):
        tracker = ServerStateTracker()
        op_id = tracker.operation_tracker.record_start("compileTest", "test.ivy")
        tracker.operation_tracker.record_end(
            op_id, success=True, message="OK", duration=10.0
        )
        history = tracker.operation_tracker.get_history()
        assert history[0].duration == 10.0

    def test_none_op_id_is_safe(self):
        """When state_tracker is None, op_id is None and track_end is a noop."""
        tracker = ServerStateTracker()
        # Simulating what happens when op_id is None (no tracker)
        tracker.operation_tracker.record_end(
            "nonexistent", success=False, message="err", duration=0
        )
        assert len(tracker.operation_tracker.get_history()) == 0
