# Aeynis Bridge MCP Server

Structured tool access for Aeynis via Model Context Protocol (MCP).

## Overview

The Bridge MCP Server wraps Aeynis's existing tools (writing, calendar) into structured MCP tools with JSON schemas. This gives her reliable, persistent tool access that doesn't depend on regex parsing or prompt engineering.

## Architecture

```
KoboldCpp (Mistral-Nemo) ←→ STDIO ←→ bridge-server.py ←→ File System
                                           ↓
                                    ~/.aeynis-state/state.json
```

## Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `write_document` | Create or edit documents | `filename`, `content`, `append` (optional) |
| `read_document` | View document contents | `filename` |
| `list_documents` | Show all documents | none |
| `calendar_add_event` | Create calendar event | `title`, `date`, `time` (opt), `description` (opt) |
| `calendar_list_events` | Query upcoming events | `days_ahead` (default 7) |
| `get_time` | Get current system time | none |

## Resources

| Resource | URI | Description |
|----------|-----|-------------|
| Writing Index | `bridge://writing_index` | JSON list of all documents |
| Calendar Summary | `bridge://calendar_summary` | Upcoming events overview |

## Setup

### Prerequisites

```bash
pip install mcp
```

### Test Infrastructure

```bash
bash ~/bridge/test-mcp.sh
```

### Test with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python3 ~/bridge/bridge-server.py
```

### Launch KoboldCpp with MCP

```bash
bash ~/bridge/launch-aeynis.sh
```

Or manually:

```bash
cd ~/koboldcpp
python3 koboldcpp.py Mistral-Nemo-Instruct-2407-Q4_K_M.gguf \
    --usecublas --gpulayers 40 \
    --mcp_server "stdio:python3 ~/bridge/bridge-server.py"
```

## File Locations

| Item | Path |
|------|------|
| MCP Server | `~/bridge/bridge-server.py` |
| Writings | `~/AeynisLibrary/writings/` |
| Calendar | `~/AeynisLibrary/calendar/events.json` |
| State | `~/.aeynis-state/state.json` |

## State Persistence

The server maintains state in `~/.aeynis-state/state.json`. This file:
- Survives KoboldCpp restarts
- Updates automatically when tools are called
- Tracks document index and calendar cache
- Is human-readable JSON

## Adding New Tools

1. Add the tool definition to `list_tools()` in `bridge-server.py`
2. Add the handler in `call_tool()`
3. Update state management if needed
4. Restart the MCP server

Example:

```python
Tool(
    name="my_new_tool",
    description="What it does",
    inputSchema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "What this param is"},
        },
        "required": ["param1"],
    },
)
```

## Troubleshooting

**MCP not detected in KoboldCpp:** Need v1.106+ with MCP support. Check `python3 koboldcpp.py --help | grep mcp`.

**Server won't start:** Check `pip install mcp` was run successfully. Test with `python3 bridge-server.py --test`.

**Tools not appearing:** Verify STDIO connection is working. Test with MCP Inspector first.

**State file issues:** Delete `~/.aeynis-state/state.json` to reset. It will be recreated on next tool call.
