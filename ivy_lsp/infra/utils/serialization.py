"""Serialization utilities for ivy-lsp."""

import logging
import os
from typing import Any


def write_locked_pickle(
    index_dir: str,
    filename: str,
    obj: Any,
    logger: logging.Logger,
) -> bool:
    """Write gzipped pickle to *index_dir*/*filename* with fcntl non-blocking lock.

    Returns True if write succeeded, False on lock contention or OS error.
    """
    import fcntl
    import gzip
    import pickle

    lock_path = os.path.join(index_dir, ".build.lock")
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out_path = os.path.join(index_dir, filename)
        with gzip.open(out_path, "wb") as f:
            pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
        logger.info("Wrote %s to %s", filename, out_path)
        return True
    except BlockingIOError:
        logger.debug("Lock held for %s, skipping write", filename)
        return False
    except OSError:
        logger.debug("Cannot write %s to %s", filename, index_dir, exc_info=True)
        return False
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()
