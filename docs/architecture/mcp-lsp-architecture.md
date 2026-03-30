# MCP/LSP Architecture: ivy-lsp in panther-ivy-plugin

How the Ivy Language Server's LSP and MCP protocols interact when loaded via Claude Code's `--plugin-dir`.

## 1. Overview

When Claude Code loads `panther-ivy-plugin` via `--plugin-dir`, three server processes are spawned:

| # | Process | Transport | Config Source |
|---|---|---|---|
| 1 | **LSP** + MCP HTTP sidecar | stdio (LSP) + HTTP (sidecar) | `.lsp.json` |
| 2 | **Standalone MCP** (ivy-tools) | stdio | `.mcp.json` |
| 3 | **Serena MCP** | stdio | `.mcp.json` |

Claude Code connects to:
- Process 1 via **stdio for LSP** (diagnostics, hover, goto-definition on `.ivy` files)
- Process 2 via **stdio for MCP** (ivy_verify, ivy_compile, ivy_coverage, etc.)
- Process 3 via **stdio for MCP** (serena tools: find_symbol, search_for_pattern, etc.)

The MCP HTTP sidecar in process 1 is **not directly accessible** from Claude Code (which only speaks stdio MCP). Instead, process 2 auto-discovers and delegates to it via the **lazy bridge upgrade mechanism** (see section 3).

## 2. Process Map

```
Claude Code
  |
  |-- stdio (LSP) ---> [Process 1: IvyLanguageServer]
  |                       |-- WorkspaceIndexer (shared)
  |                       |-- SemanticModel (shared)
  |                       |-- RequirementGraph (shared)
  |                       |-- MCP HTTP sidecar (daemon thread, port 19847+)
  |                            |-- ToolContext.from_lsp_server() (bridges LSP state)
  |                            |-- /health endpoint
  |                            |-- /mcp endpoint (Streamable HTTP)
  |                            `-- writes /tmp/ivy-mcp-{ws_hash}.port
  |
  |-- stdio (MCP) ---> [Process 2: Standalone MCP (ivy-tools)]
  |                       |-- Own WorkspaceIndexer (fallback)
  |                       |-- Own SemanticModel (fallback)
  |                       |-- sidecar-monitor thread (polls for port file)
  |                       `-- safe_tool decorator (delegates to sidecar when available)
  |
  `-- stdio (MCP) ---> [Process 3: Serena MCP]
                          `-- Independent code intelligence (find_symbol, etc.)
```

## 3. The Lazy Bridge Upgrade Mechanism

The standalone MCP (process 2) starts with its own local workspace index but automatically upgrades to delegate tool calls to the LSP's HTTP sidecar (process 1) once it becomes available.

### Startup Timeline

```
T=0   Claude Code starts all servers from .lsp.json + .mcp.json

T=1   Standalone MCP starts
      -> Builds its own local index (fallback mode)
      -> Starts "sidecar-monitor" background thread (polls every 2s)

T=2   LSP starts
      -> Indexes workspace
      -> Spawns MCP HTTP sidecar in daemon thread
      -> Sidecar binds to port 19847 (auto-increments on conflict, up to +10)
      -> Writes port file: /tmp/ivy-mcp-{sha256(workspace_root)[:12]}.port

T=5   Sidecar monitor detects port file
      -> Validates workspace root matches via GET /health
      -> Stores discovered port (does NOT connect yet)
      -> Logs: "[SIDECAR-DISCOVERED] Sidecar validated on port N"

T=?   First MCP tool call arrives from Claude Code (e.g., ivy_verify)
      -> safe_tool decorator checks: sidecar port discovered? YES
      -> Lazily connects to sidecar via streamablehttp_client
      -> Delegates tool call to sidecar, returns sidecar's result
      -> Logs: "[UPGRADED] Connected to sidecar on port N"

T=?   If sidecar crashes:
      -> Tool call fails
      -> Logs: "[DOWNGRADED] Sidecar call to X failed, using local"
      -> Falls back to local handling (own index)
      -> Monitor keeps polling, will re-upgrade when sidecar recovers
```

### Port Discovery

The sidecar writes a port file at:
```
/tmp/ivy-mcp-{workspace_hash}.port
```

Where `workspace_hash = sha256(workspace_root)[:12]`. Both processes must compute the same workspace root for the hash to match.

The sidecar monitor (`mcp/server.py:_sidecar_monitor()`) polls for this file with progressive backoff:
- First 60s: every 2s
- After 60s: every 10s
- After upgrade: every 30s (heartbeat)

### safe_tool Delegation

Every MCP tool is wrapped with the `safe_tool` decorator (`mcp/tools/__init__.py:311-400`):

```python
@safe_tool
async def ivy_verify(relative_path: str, ...):
    ...
```

On each tool call, `safe_tool`:
1. Checks `get_sidecar_client()` for an existing connection
2. If none, checks `get_sidecar_port()` (set by monitor thread)
3. If port found, lazily connects via `connect_to_sidecar(port)` (streamable HTTP)
4. Delegates `_client.call_tool(tool_name, kwargs)` to the sidecar
5. On failure: disconnects, falls through to local handling

Connection is established lazily (on first tool call, not in the monitor thread) to ensure the `ClientSession` is bound to the correct asyncio event loop.

### Upgrade/Downgrade Lifecycle

```
                  port file found + workspace validated
LOCAL (own index) ----------------------------------------> UPGRADED (sidecar delegation)
      ^                                                          |
      |                   sidecar call fails                     |
      `----------------------------------------------------------'
```

- `IVY_MCP_DISABLE_UPGRADE=1` env var disables the monitor entirely
- The sidecar reconnects on recovery (monitor heartbeat detects it)

## 4. Shared State via ToolContext

The sidecar shares the LSP's live in-memory state through `ToolContext.from_lsp_server()` (`mcp/context.py`):

| State Object | Purpose | Shared? |
|---|---|---|
| `SemanticModel` | Parsed Ivy model (types, actions, invariants) | Same instance |
| `WorkspaceIndexer` | File cache, symbol table, include graph | Same instance |
| `RequirementGraph` | RFC requirement-to-assertion mapping | Same instance |
| `IncludeResolver` | File discovery and include resolution | Same instance |
| `BasenameCache` | Filename-to-path mapping | Same instance |

When upgraded, MCP tools see the **exact same state** as the LSP (real-time, including in-flight edits). When downgraded, they use the standalone MCP's own index (potentially stale).

## 5. Plugin Loading

With `--plugin-dir .../plugins/panther-ivy-plugin`:

- Claude Code loads **only** `panther-ivy-plugin` (top-level files: `.lsp.json`, `.mcp.json`, agents/, skills/, etc.)
- The sibling `ivy-lsp` sub-plugin is **NOT loaded** (it lives in `plugins/ivy-lsp/`, not recursed)
- `marketplace.json` in the parent directory is **ignored** by `--plugin-dir`
- Result: exactly 1 LSP server and 2 MCP servers (ivy-tools + serena)

If both sub-plugins were loaded (e.g., via two `--plugin-dir` flags), there would be duplicate LSP servers for `.ivy` files. This is the `ivy-lsp` sub-plugin's only purpose, and it should be considered redundant when `panther-ivy-plugin` is loaded.

## 6. Configuration Files

### `.lsp.json`
Starts the LSP server (default mode, includes MCP HTTP sidecar):
```bash
start-ivy-server.sh --mode lsp
```
Key env vars: `IVY_MCP_PORT=19847`, `IVY_LSP_FORCE_REINSTALL=1`

### `.mcp.json`
Defines two MCP servers:
- `ivy-tools`: `start-ivy-server.sh --mode mcp` (standalone MCP with bridge)
- `serena`: `start-serena.sh` (code intelligence via panther-serena)

Key env vars: `IVY_LSP_FORCE_REINSTALL=1`, `IVY_LSP_PREWARM_MODEL=1`

### `start-ivy-server.sh`
Unified launch script for both modes. Responsibilities:
1. Parse `--mode lsp|mcp`
2. Source `workspace-common.sh` for workspace detection
3. Set up logging (session-aware log redirection)
4. Resolve ivy-lsp source (local dev > submodule > remote)
5. Kill stale servers of same mode/workspace
6. `exec uvx` with appropriate flags

### `workspace-common.sh`
Shared functions:
- `detect_ivy_workspace()`: Walk up from `$PWD`, canonicalize via `os.path.realpath()`
- `resolve_ivy_lsp_source()`: Find local ivy-lsp (IVY_LSP_DEV_ROOT > submodule > walk-up)
- `resolve_session_id()`: Python canonical > env vars > file fallback

## 7. Workspace Root Agreement

For the bridge to work, both processes must compute the same `workspace_hash`:

| Process | Workspace Root Source |
|---|---|
| LSP sidecar | `ToolContext.from_lsp_server()` -> LSP indexer -> `uri_to_path(workspace_folders[0].uri)` from Claude Code, with fallback to `IVY_WORKSPACE_ROOT` env var |
| Standalone MCP | `start-ivy-server.sh` -> `detect_ivy_workspace()` -> walks up from `$PWD`, canonicalized via `os.path.realpath()` |

**Safety nets:**
- `workspace-common.sh:74` canonicalizes the detected root: `python3 -c "import os; print(os.path.realpath(...))"`
- `validate_sidecar_workspace()` in `client.py:85-99` uses `os.path.realpath()` on both sides before comparing

**Risk:** The port file path uses the raw (non-canonicalized) hash. If the LSP and MCP have different string representations of the same path (e.g., symlink vs real path), the hash won't match and the bridge won't activate. The `validate_sidecar_workspace()` check only runs AFTER the port file is found.

## 8. Runtime Verification

### Check sidecar is running
```bash
# Port file exists?
ls /tmp/ivy-mcp-*.port

# Read port
cat /tmp/ivy-mcp-*.port

# Health endpoint
curl -s http://127.0.0.1:$(cat /tmp/ivy-mcp-*.port)/health | python3 -m json.tool
```

### Check bridge is active
```bash
# Look for upgrade in MCP logs
grep -iE "UPGRADED|SIDECAR|DOWNGRADED|DISCOVERED" /tmp/ivy-mcp-latest.log
```

Expected when working:
```
[SIDECAR-MONITOR] Started background upgrade monitor
[SIDECAR-DISCOVERED] Sidecar validated on port 19847
[UPGRADED] Connected to sidecar on port 19847 from MCP event loop
```

### Check workspace agreement
```bash
# Sidecar's workspace
curl -s http://127.0.0.1:$(cat /tmp/ivy-mcp-*.port)/health | python3 -c "import sys,json; print(json.load(sys.stdin)['workspace_root'])"

# Standalone MCP's workspace
grep "workspace" /tmp/ivy-mcp-latest.log | head -3
```

## 9. Known Issues

1. **Missing `IVY_LSP_FORCE_REINSTALL` in `.mcp.json`** (fixed 2026-03-26): Without this flag, `uvx` uses stale cached code causing `safe_tool()` signature mismatch crash.

2. **Stale port files**: If the LSP dies without cleanup, `/tmp/ivy-mcp-*.port` points to a dead port. The monitor's `validate_sidecar_workspace()` health check catches this (returns False when unreachable), but the file persists.

3. **Worktree path mismatches**: In git worktrees, the LSP (getting path from Claude Code's workspace URI) and standalone MCP (walking up from `$PWD`) may compute different workspace roots, preventing bridge activation.

4. **Port auto-increment**: If port 19847 is in use, the sidecar binds to 19848+. The port file reflects the actual port, so the monitor finds it. But external tools hardcoding 19847 will fail.

## 10. Potential Optimizations

1. **Free local index after upgrade**: Once delegating to sidecar, the standalone MCP's `SemanticModel` and `WorkspaceIndexer` sit unused in memory. They could be garbage-collected.

2. **Skip indexing when sidecar exists**: If the port file exists at MCP startup and the sidecar health check passes, skip building the local index entirely.

3. **Port file cleanup on shutdown**: `start-ivy-server.sh` cleans up PID files on exit but not the port file. Adding port file cleanup to the SIGTERM handler would prevent stale entries.

4. **Canonicalize port file hash**: Use `os.path.realpath()` before computing `workspace_hash` in both `sidecar.py` and `server.py` to prevent worktree mismatches.

## 11. Key Files Reference

| File | Role |
|---|---|
| `ivy_lsp/__main__.py` | Dual-mode entry point: `--mcp` = standalone, default = LSP + sidecar |
| `ivy_lsp/mcp/sidecar.py` | HTTP sidecar: port binding, port file, health endpoint, uvicorn |
| `ivy_lsp/mcp/client.py` | Sidecar client: port file reading, health validation, connect/disconnect |
| `ivy_lsp/mcp/context.py` | `ToolContext.from_lsp_server()` bridge between LSP and MCP state |
| `ivy_lsp/mcp/server.py` | Standalone MCP: `start_mcp()`, `_sidecar_monitor()`, `create_mcp_app()` |
| `ivy_lsp/mcp/tools/__init__.py` | `safe_tool` decorator with sidecar delegation logic |
| `ivy_lsp/lsp/server.py` | `IvyLanguageServer` with `start_mcp_sidecar()` |
| `ivy_lsp/lsp/server_setup.py` | `_setup_indexer()` — LSP workspace root resolution |
| `plugins/panther-ivy-plugin/.mcp.json` | MCP server definitions (ivy-tools + serena) |
| `plugins/panther-ivy-plugin/.lsp.json` | LSP server definition with sidecar port config |
| `plugins/panther-ivy-plugin/scripts/start-ivy-server.sh` | Unified launch script (both modes) |
| `plugins/panther-ivy-plugin/scripts/workspace-common.sh` | Shared workspace detection functions |
