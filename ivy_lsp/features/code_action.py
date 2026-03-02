"""textDocument/codeAction feature handler.

Provides quick-fix code actions for known diagnostic codes:
- ``missing-lang-header``: Insert ``#lang ivy1.7`` at the top
- ``unresolved-include``: Remove the offending include line
"""

from __future__ import annotations

import logging
from typing import List, Sequence

from lsprotocol import types as lsp

from ivy_lsp.utils.position_utils import make_range

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

        if code == "missing-lang-header":
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

        elif code == "unresolved-include":
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
            return compute_code_actions(uri, source, params.context.diagnostics)
        except Exception:
            logger.warning("code_action handler failed", exc_info=True)
            return []
