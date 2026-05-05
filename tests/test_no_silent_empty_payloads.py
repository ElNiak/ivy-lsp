"""Regression test for the silent-empty MCP payload bug.

Observed in session 5611907a-131f-422f-a908-e07a141fc452 (2026-05-04),
debug-log lines 3879, 3889, 4130, 4230. Diagnostic memo at
.claude/plans/brainstorm-analyse-deeply-effervescent-lightning.md §4.

Every read-only MCP tool must return either a non-empty dict on success or an
explicit error envelope (success=False, message=...) on failure. None and {}
are forbidden return values.

The conftest sets IVY_LSP_RAW_JSON=1 (autouse `_raw_json_for_legacy_tests`),
so safe_tool's `_format_result` returns the handler dict unchanged and FastMCP
serializes it to a TextContent block — there is no CallToolResult.structuredContent
in this test setup. We therefore parse the JSON text payload via the existing
`extract_text` helper, matching the pattern used by every other MCP test
(test_mcp_traceability.py, test_mcp_lint_diagnostics.py, test_mcp_visualization.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

IVY_ROOT = Path(__file__).resolve().parent.parent
if str(IVY_ROOT) not in sys.path:
    sys.path.insert(0, str(IVY_ROOT))

from tests.helpers.mcp_helpers import extract_text, get_mcp_app  # noqa: E402

READ_ONLY_TOOLS_AND_ARGS = [
    ("ivy_workspace", {"action": "get"}),
    ("ivy_workspace", {"action": "list"}),
    ("ivy_status", {"mode": "health"}),
    ("ivy_status", {"mode": "capabilities"}),
    ("ivy_coverage", {"mode": "stats"}),
    ("ivy_manifest", {}),
]


@pytest.mark.parametrize("tool_name,kwargs", READ_ONLY_TOOLS_AND_ARGS)
async def test_tool_returns_non_empty_payload_or_explicit_error(
    tmp_path: Path,
    tool_name: str,
    kwargs: dict[str, str],
) -> None:
    """Pin the honest-payload contract on every read-only MCP tool.

    Every read-only tool must return either a non-empty success payload or
    an explicit error envelope. None and {} are forbidden.
    """
    mcp = get_mcp_app(workspace_root=str(tmp_path))
    result = await mcp.call_tool(tool_name, kwargs)

    assert (
        result is not None
    ), f"{tool_name}({kwargs}) returned None — silent data loss."

    text = extract_text(result)
    assert text and text.strip() not in ("", "{}", "null"), (
        f"{tool_name}({kwargs}) returned an empty/null text payload "
        f"(extract_text -> {text!r})."
    )

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        pytest.fail(
            f"{tool_name}({kwargs}) returned non-JSON payload: {text!r} ({exc})"
        )

    assert isinstance(parsed, dict), (
        f"{tool_name}({kwargs}) returned a non-dict JSON payload: "
        f"{type(parsed).__name__} = {parsed!r}"
    )
    assert parsed != {}, (
        f"{tool_name}({kwargs}) returned an empty dict — the silent-empty "
        "payload bug. Either the handler returned None/{} or the wrapper "
        "discarded the payload."
    )

    if parsed.get("success") is False:
        assert "message" in parsed, (
            f"{tool_name}({kwargs}) returned success=False without a message: "
            f"{parsed!r}"
        )
    else:
        informative_keys = set(parsed) - {"success", "_context"}
        assert informative_keys, (
            f"{tool_name}({kwargs}) returned success=True with no informative "
            f"keys (only {sorted(set(parsed))!r})."
        )


@pytest.mark.parametrize("tool_name,kwargs", READ_ONLY_TOOLS_AND_ARGS)
async def test_tool_structuredcontent_survives_production_format_path(
    tmp_path: Path,
    tool_name: str,
    kwargs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-path counterpart to the test above.

    The session-5611907a debug log showed empty {} payloads to PostToolUse
    hooks. The hooks read ``CallToolResult.structuredContent``. In the
    production code path (IVY_LSP_RAW_JSON unset), ``safe_tool`` calls
    ``_format_result`` which converts the handler's dict into a markdown
    string, then wraps it in a CallToolResult with both ``content=[markdown]``
    and ``structuredContent=raw_dict``. This test asserts that the raw dict
    survives the formatting step — i.e. structuredContent is non-empty.

    A failure of this test (combined with the other test passing) localises
    the bug to ``_format_result`` / ``format_tool_result``. A pass would push
    the investigation downstream to FastMCP, the MCP transport, or the
    PostToolUse hook.
    """
    monkeypatch.delenv("IVY_LSP_RAW_JSON", raising=False)
    mcp = get_mcp_app(workspace_root=str(tmp_path))
    result = await mcp.call_tool(tool_name, kwargs)

    assert (
        result is not None
    ), f"{tool_name}({kwargs}) returned None from production path."

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        # Some FastMCP test-app return shapes wrap the result; fall back to
        # the text payload to confirm the bug is at the envelope layer.
        text = extract_text(result)
        msg = (
            f"{tool_name}({kwargs}) production path: no structuredContent"
            f" attribute. Text fallback: {text!r}."
            f" This means the envelope did not include structuredContent —"
            f" bug is in safe_tool's CallToolResult construction."
        )
        pytest.fail(msg)

    assert structured != {}, (
        f"{tool_name}({kwargs}) production path: structuredContent is empty "
        "dict. Bug is in _format_result or format_tool_result discarding the "
        "handler payload."
    )

    if structured.get("success") is False:
        assert "message" in structured, (
            f"{tool_name}({kwargs}) production path: success=False without "
            f"message: {structured!r}"
        )
    else:
        informative_keys = set(structured) - {"success", "_context"}
        assert informative_keys, (
            f"{tool_name}({kwargs}) production path: success=True with no "
            f"informative keys (only {sorted(set(structured))!r})."
        )
