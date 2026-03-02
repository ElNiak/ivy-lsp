"""Subprocess worker for isolated Ivy compilation.

Launched indirectly via ``compiler_manager._worker_entry()`` trampoline.
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

        # --- ivyc-equivalent initialization ---
        # These match what ivy_to_cpp.main_int does for target=test.
        # Without them, ivy_from_string() fails on symbol resolution
        # for imported actions, struct destructors, and sort interpretations.
        try:
            import ivy.ivy_actions as ia
            import ivy.ivy_isolate as iso
            import ivy.ivy_solver as slv

            ia.set_determinize(True)
            slv.set_use_native_enums(True)
            iso.set_interpret_all_sorts(True)
            ic.set_verifying(False)
        except (ImportError, AttributeError) as init_err:
            logger.debug("Optional ivyc init flags unavailable: %s", init_err)

        iu.set_parameters({
            "coi": "false",
            "create_imports": "true",
            "enforce_axioms": "true",
            "ui": "none",
            "isolate_mode": "test",
            "assume_invariants": "false",
            "keep_destructors": "true",
        })

        iu.filename = filename
        with im.Module():
            # Add _generating symbol for test target (QUIC tests use this)
            try:
                im.module.sig.add_symbol("_generating", il.BooleanSort())
            except (AttributeError, TypeError) as sym_err:
                logger.debug("Could not add _generating symbol: %s", sym_err)

            ic.ivy_from_string(source, create_isolate=False)

            duration = time.monotonic() - start

            from ivy_lsp.compilation.extractor import extract_compiled_module_ir

            ir = extract_compiled_module_ir(im.module, il.sig, filename, duration)
        result_conn.send(ir)
    except Exception as exc:
        logger.warning("Compilation failed for %s: %s", filename, exc, exc_info=True)
        duration = time.monotonic() - start
        ir = CompiledModuleIR.empty(
            filename, errors=[str(exc)], duration=duration
        )
        try:
            result_conn.send(ir)
        except OSError:
            logger.debug("Could not send error IR for %s: pipe broken", filename)
    finally:
        try:
            result_conn.close()
        except OSError:
            logger.debug("Could not close connection for %s", filename)
