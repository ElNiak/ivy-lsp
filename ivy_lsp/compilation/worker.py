"""Subprocess worker for isolated Ivy compilation.

Launched via ``multiprocessing.Process(target=compiler_worker, ...)``.
Runs the full Ivy compiler pipeline in an isolated process, extracts
a :class:`CompiledModuleIR`, and sends it back via a
``multiprocessing.Connection`` pipe.
"""

from __future__ import annotations

import logging
import os
import time
from multiprocessing.connection import Connection
from typing import Optional

from ivy_lsp.compilation.ir import CompiledModuleIR

logger = logging.getLogger(__name__)


def compiler_worker(
    source: str,
    filename: str,
    result_conn: Connection,
    staging_dir: Optional[str] = None,
) -> None:
    """Run the full Ivy compilation pipeline in an isolated process.

    1. Set CWD to *staging_dir* (for include resolution).
    2. Call ``ivy_from_string(source)`` which runs IvyDomainSetup,
       IvyConjectureSetup, IvyARGSetup.
    3. Extract :class:`CompiledModuleIR` from the populated Module.
    4. Send the IR back via *result_conn*.

    Never raises -- sends a failed IR on any exception.
    """
    start = time.monotonic()
    try:
        if staging_dir:
            os.chdir(staging_dir)

        import ivy.ivy_compiler as ic
        import ivy.ivy_module as im
        import ivy.ivy_logic as il
        import ivy.ivy_utils as iu

        iu.filename = filename
        ic.ivy_from_string(source)

        duration = time.monotonic() - start

        from ivy_lsp.compilation.extractor import extract_compiled_module_ir

        ir = extract_compiled_module_ir(im.module, il.sig, filename, duration)
        result_conn.send(ir)
    except Exception as exc:
        duration = time.monotonic() - start
        ir = CompiledModuleIR.empty(
            filename, errors=[str(exc)], duration=duration
        )
        try:
            result_conn.send(ir)
        except Exception:
            pass  # Connection may be broken
    finally:
        try:
            result_conn.close()
        except Exception:
            pass
