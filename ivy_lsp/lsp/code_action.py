"""textDocument/codeAction feature handler.

Provides quick-fix code actions for known diagnostic codes:
- ``ivy.syntax.missingLangHeader``: Insert ``#lang ivy1.7`` at the top
- ``ivy.module.unresolvedInclude``: Remove the offending include line
- ``ivy.action.noMonitor``: Insert a skeleton ``after`` monitor block
- ``ivy.action.unguardedWrite``: Insert a skeleton ``require`` guard
- ``ivy.invariant.unguardedWrite``: Insert a skeleton ``require`` guard
  (mirrors action.unguardedWrite shape)
- ``ivy.action.missingFinalize``: Insert a skeleton ``export action _finalize``
- ``ivy.rfc.missingTag``: Append a ``# [rfcNNNN:X.Y]`` template to the line
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Sequence

from lsprotocol import types as lsp

from ivy_lsp.infra.utils.position_utils import make_range

logger = logging.getLogger(__name__)


def compute_code_actions(
    uri: str,
    source: str,
    diagnostics: Sequence[lsp.Diagnostic],
) -> List[lsp.CodeAction]:
    """Compute quick-fix code actions for the given diagnostics."""
    actions: List[lsp.CodeAction] = []

    for diag in diagnostics:
        if diag.code is None:
            code = None
        elif isinstance(diag.code, str):
            code = diag.code
        else:
            code = str(diag.code)

        if code == "ivy.syntax.missingLangHeader":
            actions.append(
                lsp.CodeAction(
                    title="Insert #lang ivy1.7 header",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(0, 0, 0, 0),
                                    new_text="#lang ivy1.7\n",
                                )
                            ]
                        }
                    ),
                )
            )

        elif code == "ivy.module.unresolvedInclude":
            lines = source.split("\n")
            line_no = diag.range.start.line
            if line_no < len(lines):
                if line_no + 1 < len(lines):
                    # Not the last line: delete from start to start of next
                    end_line = line_no + 1
                    end_char = 0
                else:
                    # Last line: delete to end of this line
                    end_line = line_no
                    end_char = len(lines[line_no])
                actions.append(
                    lsp.CodeAction(
                        title="Remove unresolved include",
                        kind=lsp.CodeActionKind.QuickFix,
                        diagnostics=[diag],
                        edit=lsp.WorkspaceEdit(
                            changes={
                                uri: [
                                    lsp.TextEdit(
                                        range=make_range(
                                            line_no, 0, end_line, end_char
                                        ),
                                        new_text="",
                                    )
                                ]
                            }
                        ),
                    )
                )

        elif code == "ivy.action.noMonitor":
            # Extract action name from message like "Action 'foo' has no ..."
            m = re.search(r"Action '(\w+)'", diag.message)
            action_name = m.group(1) if m else "action_name"
            insert_line = diag.range.end.line + 1
            snippet = f"\nafter {action_name} {{\n" f"    ensure ...\n" f"}}\n"
            actions.append(
                lsp.CodeAction(
                    title=f"Add after monitor for '{action_name}'",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(
                                        insert_line,
                                        0,
                                        insert_line,
                                        0,
                                    ),
                                    new_text=snippet,
                                )
                            ]
                        }
                    ),
                )
            )

        elif code == "ivy.action.unguardedWrite":
            # Extract var name from message like "State var 'foo' is written ..."
            m = re.search(r"State var '([\w.]+)'", diag.message)
            var_name = m.group(1) if m else "state_var"
            insert_line = diag.range.end.line + 1
            snippet = f"    require {var_name}(...)\n"
            actions.append(
                lsp.CodeAction(
                    title=f"Add require guard for '{var_name}'",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(
                                        insert_line,
                                        0,
                                        insert_line,
                                        0,
                                    ),
                                    new_text=snippet,
                                )
                            ]
                        }
                    ),
                )
            )

        elif code == "ivy.invariant.unguardedWrite":
            # Mirrors the ivy.action.unguardedWrite shape: extract the var
            # name from the diagnostic message and insert a `require`
            # guard skeleton. Registry title is
            # "Unguarded state variable write: '{var}'", so the message
            # carries the var name in single quotes.
            #
            # Note: ivy.invariant.unguardedWrite is declared with
            # has_quick_fix=True but no emit site uses it today (the
            # active code is ivy.action.unguardedWrite). This branch is
            # forward-looking.
            m = re.search(r"'([\w.]+)'", diag.message)
            var_name = m.group(1) if m else "state_var"
            insert_line = diag.range.end.line + 1
            snippet = f"    require {var_name}(...)\n"
            actions.append(
                lsp.CodeAction(
                    title=f"Add require guard for '{var_name}' (invariant)",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(
                                        insert_line,
                                        0,
                                        insert_line,
                                        0,
                                    ),
                                    new_text=snippet,
                                )
                            ]
                        }
                    ),
                )
            )

        elif code == "ivy.action.missingFinalize":
            # Append a skeleton `export action _finalize` block at end of
            # file. The diagnostic is file-level (line=0); end-of-file
            # placement keeps the insertion at top-level scope without
            # disrupting includes or other declarations. Keep the leading
            # newline so the skeleton is visually separated from the
            # preceding declaration.
            lines = source.split("\n")
            insert_line = max(0, len(lines) - 1)
            insert_col = len(lines[insert_line]) if lines else 0
            snippet = (
                "\n\nexport action _finalize = {\n"
                "    # End-of-test assertions go here\n"
                "}\n"
            )
            actions.append(
                lsp.CodeAction(
                    title="Add export action _finalize skeleton",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(
                                        insert_line,
                                        insert_col,
                                        insert_line,
                                        insert_col,
                                    ),
                                    new_text=snippet,
                                )
                            ]
                        }
                    ),
                )
            )

        elif code == "ivy.rfc.missingTag":
            # Append a placeholder bracket-tag template to the end of the
            # assertion line. After Phase 5, diag.range spans the assertion
            # keyword + body; we insert at end_character so the template
            # follows the `;` without overlapping the assertion text.
            #
            # Note: ivy.rfc.missingTag is declared in DIAGNOSTIC_REGISTRY but
            # no emit site uses it today (the active emission code is
            # ivy.rfc.missingBracketTag). This branch is forward-looking.
            insert_line = diag.range.end.line
            insert_col = diag.range.end.character
            snippet = "  # [rfcNNNN:X.Y]"
            actions.append(
                lsp.CodeAction(
                    title="Append RFC bracket-tag template",
                    kind=lsp.CodeActionKind.QuickFix,
                    diagnostics=[diag],
                    edit=lsp.WorkspaceEdit(
                        changes={
                            uri: [
                                lsp.TextEdit(
                                    range=make_range(
                                        insert_line,
                                        insert_col,
                                        insert_line,
                                        insert_col,
                                    ),
                                    new_text=snippet,
                                )
                            ]
                        }
                    ),
                )
            )

    return actions


def register(server) -> None:
    """Register the ``textDocument/codeAction`` feature handler."""

    @server.feature(
        lsp.TEXT_DOCUMENT_CODE_ACTION,
        lsp.CodeActionOptions(
            code_action_kinds=[lsp.CodeActionKind.QuickFix],
        ),
    )
    async def code_action(
        params: lsp.CodeActionParams,
    ) -> List[lsp.CodeAction]:
        try:
            uri = params.text_document.uri
            doc = server.workspace.get_text_document(uri)
            source = doc.source or ""
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                compute_code_actions,
                uri,
                source,
                params.context.diagnostics,
            )
        except Exception:
            logger.warning("code_action handler failed", exc_info=True)
            return []
