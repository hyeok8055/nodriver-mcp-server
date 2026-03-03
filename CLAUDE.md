# nodriver-mcp-server

## Project Overview

FastMCP server providing browser automation via nodriver (undetected Chrome / CDP).

- **Main file**: `src/nodriver_mcp/server.py` (~2400 lines, all tools in one file)
- **Framework**: `mcp.server.fastmcp.FastMCP`
- **CDP access**: `tab.send(cdp_module.method())` via `_safe_call`

## Development Commands

```bash
uv run nodriver-mcp       # run the MCP server
python -m py_compile src/nodriver_mcp/server.py   # syntax check
python -c "from nodriver_mcp.server import mcp"   # import check
```

## Architecture Patterns

### Session/Tab lifecycle

```python
session = await manager.get_session(session_id)   # look up session
async with session.lock:                           # always lock
    rec = _find_tab(session, tab_id)              # resolve tab (None = active tab)
    tab = rec.tab                                  # nodriver Tab object
```

### Return values

```python
# success
ToolResult(ok=True, session_id=..., tab_id=..., data={...}).to_dict()

# error
ToolResult.fail(session_id, tab_id, ErrorCodes.X, "message").to_dict()
```

### CDP calls

```python
await _cdp_call(tab, "Domain", "methodName", {"param": value})
# or direct:
import nodriver.cdp.network as cdp_network
await _safe_call(tab.send, cdp_network.enable())
```

## File Structure

```
src/nodriver_mcp/
  server.py          # all tools, helpers, BrowserManager, ToolResult
pyproject.toml       # mcp[cli]>=1.26.0, nodriver>=0.38
CLAUDE.md            # this file
```

## Tool Naming Conventions

| Prefix | Meaning |
|--------|---------|
| `browser_*` | Session/tab/browser-level operations |
| `page_*` | Page content reading |
| `*_by_ref` | Ref-based interaction (requires prior page_snapshot) |
| `element_*` | Low-level element operations (file upload, drag) |
| `cdp_*` | Raw Chrome DevTools Protocol access |

## Adding a New Tool

1. Decorate with `@mcp.tool(annotations=PRESET)` — pick an appropriate preset from the 10 constants defined after `ALLOWED_CDP_DOMAINS`.
2. Write a 1–2 line docstring (imperative, describes what it does and when to use it).
3. Follow the session/lock/find_tab/ToolResult pattern.
4. If the tool navigates or refreshes the page, call `session.snapshot_cache.pop(rec.tab_id, None)`.

## ToolAnnotations Presets

| Constant | Use for |
|----------|---------|
| `_READ_ONLY` | Reading page content, waiting |
| `_READ_ONLY_CLOSED` | Reading session/tab metadata |
| `_NAVIGATE` | Navigation (non-idempotent) |
| `_NAVIGATE_IDEMPOTENT` | Reload |
| `_MUTATE` | Clicks, fills, scroll, JS eval |
| `_MUTATE_IDEMPOTENT` | localStorage set, block resources |
| `_MUTATE_CLOSED` | CDP subscriptions, network capture |
| `_DESTRUCTIVE` | Close tab, clear storage |
| `_DESTRUCTIVE_CLOSED` | Stop session, cleanup stale |
| `_CREATE_CLOSED` | Start session |
