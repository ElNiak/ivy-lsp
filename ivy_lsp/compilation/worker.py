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
    2. Write source to a file and load via ``ivy_load_file`` (matching
       the ``ivyc`` code path) instead of ``ivy_from_string``.
    3. Extract :class:`CompiledModuleIR` from the populated Module.
    4. Send the IR back via *result_conn*.

    Never raises -- sends a failed IR on any exception.
    """
    start = time.monotonic()
    try:
        if staging_dir:
            os.chdir(staging_dir)

        import ivy.ivy_compiler as ic
        import ivy.ivy_logic as il
        import ivy.ivy_module as im
        import ivy.ivy_utils as iu

        # --- Step 8: Diagnostic instrumentation ---
        logger.debug(
            "DIAG: subprocess env — ivy.__file__=%s, "
            "std_include_dir=%s, "
            "ivy_version=%s, "
            "cwd=%s, "
            "staging_dir=%s",
            getattr(__import__("ivy"), "__file__", "UNKNOWN"),
            iu.get_std_include_dir(),
            iu.get_string_version(),
            os.getcwd(),
            staging_dir,
        )
        # Check stdlib files exist
        std_dir = iu.get_std_include_dir()
        for name in ("collections.ivy", "collections_impl.ivy", "order.ivy"):
            path = os.path.join(std_dir, name)
            logger.debug("DIAG: stdlib %s exists=%s", path, os.path.isfile(path))

        # Monkey-patch import_module to trace every include resolution
        _orig_import_module = getattr(ic, "import_module", None)

        if _orig_import_module is not None:

            def _traced_import_module(name: str):  # type: ignore[no-untyped-def]
                fname = name + ".ivy"
                cwd_path = os.path.join(os.getcwd(), fname)
                std_path = os.path.join(iu.get_std_include_dir(), fname)
                logger.debug(
                    "DIAG: import_module(%r) — cwd_exists=%s std_exists=%s",
                    name,
                    os.path.isfile(cwd_path),
                    os.path.isfile(std_path),
                )
                try:
                    result = _orig_import_module(name)
                    logger.debug("DIAG: import_module(%r) — SUCCESS", name)
                    return result
                except Exception as exc:
                    logger.debug("DIAG: import_module(%r) — FAILED: %s", name, exc)
                    raise

            ic.import_module = _traced_import_module  # type: ignore[attr-defined]

        # --- Step 9: Symlink stdlib files into staging directory ---
        # Ensures import_module() finds stdlib files in CWD, bypassing
        # get_std_include_dir() which may fail in subprocess contexts.
        if staging_dir:
            if std_dir and os.path.isdir(std_dir):
                staged_count = 0
                for fn in os.listdir(std_dir):
                    if fn.endswith(".ivy"):
                        link_path = os.path.join(staging_dir, fn)
                        if not os.path.exists(link_path):
                            try:
                                os.symlink(os.path.join(std_dir, fn), link_path)
                                staged_count += 1
                            except OSError as exc:
                                logger.debug("Could not symlink stdlib %s: %s", fn, exc)
                logger.info("Staged %d stdlib files from %s", staged_count, std_dir)
            else:
                logger.warning("Could not locate stdlib include dir: %s", std_dir)

        # --- ivyc-equivalent initialization ---
        # These match what ivy_to_cpp.main_int does for target=test.
        # Without them, ivy_from_string() fails on symbol resolution
        # for imported actions, struct destructors, and sort interpretations.
        iso = None
        ivy_ast = None
        try:
            import ivy.ivy_actions as ia
            import ivy.ivy_ast as ivy_ast
            import ivy.ivy_isolate as iso
            import ivy.ivy_solver as slv

            ia.set_determinize(True)
            slv.set_use_native_enums(True)
            iso.set_interpret_all_sorts(True)
            ic.set_verifying(False)
        except (ImportError, AttributeError) as init_err:
            logger.debug("Optional ivyc init flags unavailable: %s", init_err)

        iu.set_parameters(
            {
                "coi": "false",
                "create_imports": "true",
                "enforce_axioms": "true",
                "ui": "none",
                "isolate_mode": "test",
                "assume_invariants": "false",
                "keep_destructors": "true",
            }
        )

        # Write source to a file in CWD so ivy_load_file sees a real
        # file object, matching ivyc's source_file() code path exactly.
        # This avoids ivy_from_string()'s StringIO path which causes
        # "vector undefined in instantiation" errors due to PLY parser
        # state divergence in subprocess contexts.
        basename = os.path.basename(filename)
        source_path = os.path.join(
            staging_dir if staging_dir else os.getcwd(), basename
        )
        with open(source_path, "w") as wf:
            wf.write(source)

        with im.Module():
            # Add _generating symbol for test target (QUIC tests use this)
            try:
                im.module.sig.add_symbol("_generating", il.BooleanSort())
            except (AttributeError, TypeError) as sym_err:
                logger.debug("Could not add _generating symbol: %s", sym_err)

            # Match ivyc's source_file(): SourceFile context + real file
            with iu.SourceFile(source_path):
                with open(source_path, "r") as sf:
                    ic.ivy_load_file(sf, create_isolate=False)
                try:
                    im.module.name = basename[: basename.rindex(".")]
                except ValueError:
                    im.module.name = basename

            # Isolate selection (outside SourceFile context, matching ivyc)
            isolate = ic.isolate.get()
            if isolate is not None:
                isolates = [isolate]
            else:
                extracts = [
                    (x, y)
                    for x, y in im.module.isolates.items()
                    if isinstance(y, ivy_ast.ExtractDef)
                ]
                if not extracts:
                    isol = ivy_ast.ExtractDef(
                        ivy_ast.Atom("extract"), ivy_ast.Atom("this")
                    )
                    isol.with_args = 1
                    im.module.isolates["extract"] = isol
                    isolates = ["extract"]
                else:
                    isolates = [ex[0] for ex in extracts]

            # Per-isolate processing (matching ivyc line 8747)
            try:
                if isolates and iso is not None:
                    iso.create_isolate(isolates[0])
                    im.module.labeled_axioms.extend(im.module.labeled_props)
                    im.module.labeled_props = []
            except Exception as iso_err:
                logger.debug(
                    "Isolate creation failed (non-fatal for LSP): %s",
                    iso_err,
                )

            duration = time.monotonic() - start

            from ivy_lsp.compilation.extractor import extract_compiled_module_ir

            ir = extract_compiled_module_ir(im.module, il.sig, filename, duration)
        result_conn.send(ir)
    except Exception as exc:
        logger.warning("Compilation failed for %s: %s", filename, exc, exc_info=True)
        duration = time.monotonic() - start
        ir = CompiledModuleIR.empty(filename, errors=[str(exc)], duration=duration)
        try:
            result_conn.send(ir)
        except OSError:
            logger.debug("Could not send error IR for %s: pipe broken", filename)
    finally:
        try:
            result_conn.close()
        except OSError:
            logger.debug("Could not close connection for %s", filename)
