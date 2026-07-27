"""Server state tracking for monitoring endpoints."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class IndexingState(Enum):
    """Possible states of the workspace indexer."""

    IDLE = "idle"
    INDEXING = "indexing"
    ERROR = "error"


@dataclass
class OperationRecord:
    """Snapshot of a single tracked operation with timing and outcome."""

    type: str
    file: Optional[str]
    start_time: float
    duration: Optional[float]
    success: Optional[bool]
    message: str


class OperationTracker:
    """In-memory ring buffer of operations with active tracking.

    All public methods are thread-safe (guarded by an internal lock).
    """

    def __init__(self, max_history: int = 20) -> None:
        """Initialize with empty active map and bounded history buffer."""
        self._active: Dict[str, OperationRecord] = {}
        self._history: deque[OperationRecord] = deque(maxlen=max_history)
        self._lock = threading.Lock()

    def record_start(self, op_type: str, file: Optional[str] = None) -> str:
        """Start tracking an operation and return its unique ID."""
        op_id = uuid.uuid4().hex[:12]
        record = OperationRecord(
            type=op_type,
            file=file,
            start_time=time.time(),
            duration=None,
            success=None,
            message="",
        )
        with self._lock:
            self._active[op_id] = record
        return op_id

    def record_end(
        self,
        op_id: str,
        success: bool,
        message: str,
        duration: float,
    ) -> None:
        """Finalize an operation and move it to the history buffer."""
        with self._lock:
            record = self._active.pop(op_id, None)
            if record is None:
                return
            record.success = success
            record.message = message
            record.duration = duration
            self._history.appendleft(record)

    def get_active(self) -> List[OperationRecord]:
        """Return a snapshot of currently active operations."""
        with self._lock:
            return list(self._active.values())

    def get_history(self, limit: int = 20) -> List[OperationRecord]:
        """Return recent completed operations, newest first."""
        with self._lock:
            return list(self._history)[:limit]


class ServerStateTracker:
    """Aggregates all server state for monitoring queries.

    Multi-attribute writes and reads are guarded by a lock to prevent
    torn reads (e.g. ``IDLE`` state with ``None`` duration).
    """

    def __init__(self) -> None:
        """Initialize server state with default idle values."""
        self._lock = threading.Lock()
        self.start_time: float = time.time()
        self.indexing_state: IndexingState = IndexingState.IDLE
        self.indexing_error: Optional[str] = None
        self.operation_tracker: OperationTracker = OperationTracker()
        self.last_index_time: Optional[float] = None
        self.last_index_duration: Optional[float] = None

    @property
    def uptime_seconds(self) -> float:
        """Return seconds elapsed since server start."""
        return time.time() - self.start_time

    def set_indexing(self) -> None:
        """Transition state to INDEXING."""
        with self._lock:
            self.indexing_state = IndexingState.INDEXING
            self.indexing_error = None

    def set_indexed(self, duration: float) -> None:
        """Transition state to IDLE after successful indexing."""
        with self._lock:
            self.indexing_state = IndexingState.IDLE
            self.last_index_duration = duration
            self.last_index_time = time.time()

    def set_index_error(self, error: str) -> None:
        """Transition state to ERROR with an error message."""
        with self._lock:
            self.indexing_state = IndexingState.ERROR
            self.indexing_error = error

    def to_status_dict(
        self,
        mode: str,
        version: str,
        tools: Dict[str, bool],
    ) -> Dict:
        """Build a status summary dict for monitoring endpoints."""
        # Snapshot active ops first (acquires OperationTracker._lock only)
        active_records = self.operation_tracker.get_active()
        active_ops = [
            {
                "type": op.type,
                "file": op.file,
                "elapsed": round(time.time() - op.start_time, 1),
            }
            for op in active_records
        ]
        # Then read own state (acquires ServerStateTracker._lock only)
        with self._lock:
            return {
                "mode": mode,
                "version": version,
                "uptimeSeconds": round(self.uptime_seconds, 1),
                "indexingState": self.indexing_state.value,
                "indexingError": self.indexing_error,
                "toolAvailability": tools,
                "activeOperations": active_ops,
            }
