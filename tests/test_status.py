"""Tests for server state tracking: OperationTracker, IndexingState, ServerStateTracker."""

import pytest

from ivy_lsp.lsp.ui.status import (
    IndexingState,
    OperationRecord,
    OperationTracker,
    ServerStateTracker,
)


class TestIndexingState:
    def test_enum_values(self):
        assert IndexingState.IDLE.value == "idle"
        assert IndexingState.INDEXING.value == "indexing"
        assert IndexingState.ERROR.value == "error"


class TestOperationRecord:
    def test_creation(self):
        rec = OperationRecord(
            type="verify",
            file="test.ivy",
            start_time=1000.0,
            duration=None,
            success=None,
            message="",
        )
        assert rec.type == "verify"
        assert rec.file == "test.ivy"
        assert rec.duration is None
        assert rec.success is None


class TestOperationTracker:
    def test_record_start_returns_id(self):
        tracker = OperationTracker()
        op_id = tracker.record_start("verify", "test.ivy")
        assert isinstance(op_id, str)
        assert len(op_id) > 0

    def test_active_operations(self):
        tracker = OperationTracker()
        tracker.record_start("verify", "test.ivy")
        active = tracker.get_active()
        assert len(active) == 1
        assert active[0].type == "verify"
        assert active[0].file == "test.ivy"
        assert active[0].success is None

    def test_record_end_moves_to_history(self):
        tracker = OperationTracker()
        op_id = tracker.record_start("verify", "test.ivy")
        tracker.record_end(op_id, success=True, message="OK", duration=1.5)
        assert len(tracker.get_active()) == 0
        history = tracker.get_history()
        assert len(history) == 1
        assert history[0].success is True
        assert history[0].duration == 1.5

    def test_history_ring_buffer_limit(self):
        tracker = OperationTracker(max_history=3)
        for i in range(5):
            op_id = tracker.record_start("verify", f"file{i}.ivy")
            tracker.record_end(op_id, success=True, message="OK", duration=0.1)
        history = tracker.get_history()
        assert len(history) == 3
        # Most recent first
        assert history[0].file == "file4.ivy"

    def test_record_end_unknown_id_is_noop(self):
        tracker = OperationTracker()
        tracker.record_end("nonexistent", success=False, message="err", duration=0)
        assert len(tracker.get_history()) == 0

    def test_multiple_active_operations(self):
        tracker = OperationTracker()
        id1 = tracker.record_start("verify", "a.ivy")
        id2 = tracker.record_start("compile", "b.ivy")
        assert len(tracker.get_active()) == 2
        tracker.record_end(id1, success=True, message="OK", duration=1.0)
        assert len(tracker.get_active()) == 1
        assert tracker.get_active()[0].type == "compile"

    def test_get_history_with_limit(self):
        tracker = OperationTracker()
        for i in range(10):
            op_id = tracker.record_start("verify", f"file{i}.ivy")
            tracker.record_end(op_id, success=True, message="OK", duration=0.1)
        assert len(tracker.get_history(limit=5)) == 5


class TestServerStateTracker:
    def test_initial_state(self):
        tracker = ServerStateTracker()
        assert tracker.indexing_state == IndexingState.IDLE
        assert tracker.indexing_error is None
        assert tracker.last_index_time is None
        assert tracker.last_index_duration is None
        assert isinstance(tracker.operation_tracker, OperationTracker)

    def test_uptime(self):
        tracker = ServerStateTracker()
        assert tracker.uptime_seconds >= 0
        assert tracker.uptime_seconds < 1.0

    def test_set_indexing(self):
        tracker = ServerStateTracker()
        tracker.set_indexing()
        assert tracker.indexing_state == IndexingState.INDEXING

    def test_set_indexed(self):
        tracker = ServerStateTracker()
        tracker.set_indexing()
        tracker.set_indexed(duration=1.5)
        assert tracker.indexing_state == IndexingState.IDLE
        assert tracker.last_index_duration == 1.5
        assert tracker.last_index_time is not None

    def test_set_index_error(self):
        tracker = ServerStateTracker()
        tracker.set_indexing()
        tracker.set_index_error("Parse failed")
        assert tracker.indexing_state == IndexingState.ERROR
        assert tracker.indexing_error == "Parse failed"

    def test_to_dict(self):
        tracker = ServerStateTracker()
        d = tracker.to_status_dict(
            mode="full",
            version="0.8.0",
            tools={"ivyCheck": True, "ivyc": False, "ivyShow": True},
        )
        assert d["mode"] == "full"
        assert d["version"] == "0.8.0"
        assert d["indexingState"] == "idle"
        assert "uptimeSeconds" in d
        assert d["toolAvailability"]["ivyCheck"] is True
        assert d["activeOperations"] == []


class TestStatusDictAtomicity:
    """H4: get_active() must be called before acquiring self._lock to avoid nested locking."""

    def test_active_ops_computed_before_lock(self):
        """get_active() must NOT be called while self._lock is held.

        Acquiring OperationTracker._lock inside ServerStateTracker._lock would
        create a nested locking order that risks deadlock if any code path ever
        acquires the same two locks in the reverse order.  The fix snapshots
        active operations first (OperationTracker._lock only), then reads own
        state (ServerStateTracker._lock only), keeping the two locks disjoint.
        """
        tracker = ServerStateTracker()
        lock_was_held = []

        original_get_active = tracker.operation_tracker.get_active

        def tracked_get_active():
            lock_was_held.append(tracker._lock.locked())
            return original_get_active()

        tracker.operation_tracker.get_active = tracked_get_active
        tracker.to_status_dict("full", "0.8.0", {})

        assert lock_was_held == [False], (
            "get_active() must be called before self._lock is acquired to "
            "eliminate nested locking (H4)"
        )
