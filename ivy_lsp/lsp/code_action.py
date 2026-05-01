"""textDocument/codeAction feature handler.

Provides quick-fix code actions for known diagnostic codes:
- ``ivy.syntax.missingLangHeader``: Insert ``#lang ivy1.7`` at the top
- ``ivy.module.unresolvedInclude``: Remove the offending include line
- ``ivy.no-monitor``: Insert a skeleton ``after`` monitor block
- ``ivy.unguarded-write``: Insert a skeleton ``require`` guard
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

        elif code == "ivy.no-monitor":
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

        elif code == "ivy.unguarded-write":
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
