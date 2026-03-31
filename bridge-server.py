#!/usr/bin/env python3
"""
Aeynis Bridge MCP Server - Structured tool access for Aeynis

A Model Context Protocol (MCP) server that wraps Aeynis's existing tools
(writing, calendar) with proper JSON schemas and structured responses.
Runs on STDIO transport — no HTTP required.

Usage:
  python bridge-server.py              # Full server with all tools
  python bridge-server.py --test       # Minimal test server (get_time only)

KoboldCpp integration:
  ./koboldcpp --model mistral-nemo.gguf --mcp_server stdio:"python bridge-server.py"

Tools:
  write_document  - Create/edit documents via AbiWord or direct file I/O
  read_document   - View document contents
  list_documents  - Show all documents Aeynis has created
  calendar_add    - Create a calendar event
  calendar_list   - Query upcoming events
  get_time        - Get current system time (test tool)

Resources:
  writing_index    - Persistent list of all documents
  calendar_summary - Upcoming events overview

State: ~/.aeynis-state/state.json (persists across KoboldCpp restarts)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# MCP SDK imports
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool, Resource
except ImportError:
    print("ERROR: MCP SDK not installed. Run: pip install mcp", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("bridge-server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LIBRARY_ROOT = os.path.expanduser("~/AeynisLibrary")
WRITINGS_DIR = os.path.join(LIBRARY_ROOT, "writings")
CALENDAR_DIR = os.path.join(LIBRARY_ROOT, "calendar")
CALENDAR_FILE = os.path.join(CALENDAR_DIR, "events.json")
STATE_DIR = os.path.expanduser("~/.aeynis-state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# Ensure directories exist
for d in [WRITINGS_DIR, CALENDAR_DIR, STATE_DIR]:
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# Persistent State
# ---------------------------------------------------------------------------
class BridgeState:
    """Manages persistent state that survives KoboldCpp restarts."""

    def __init__(self):
        self.state = self._load()

    def _load(self) -> dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"documents": [], "calendar_cache": {"last_updated": None, "events": []}}

    def save(self):
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            logger.error(f"Failed to save state: {e}")

    def refresh_documents(self):
        """Scan writings directory and update document index."""
        docs = []
        if os.path.isdir(WRITINGS_DIR):
            for entry in sorted(os.scandir(WRITINGS_DIR), key=lambda e: e.name):
                if entry.is_file():
                    stat = entry.stat()
                    docs.append({
                        "filename": entry.name,
                        "path": entry.path,
                        "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                        "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "size_bytes": stat.st_size,
                    })
        self.state["documents"] = docs
        self.save()
        return docs

    def refresh_calendar(self):
        """Load calendar events from the JSON file."""
        events = []
        if os.path.exists(CALENDAR_FILE):
            try:
                with open(CALENDAR_FILE, "r") as f:
                    events = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        self.state["calendar_cache"] = {
            "last_updated": datetime.now().isoformat(),
            "events": events,
        }
        self.save()
        return events


# ---------------------------------------------------------------------------
# Document Operations
# ---------------------------------------------------------------------------
def write_document_to_disk(filename: str, content: str, append: bool = False) -> dict:
    """Write or append to a document in the writings directory."""
    # Sanitize filename
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ").strip()
    if not safe_name:
        safe_name = f"writing_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not safe_name.endswith(".md"):
        safe_name += ".md"

    filepath = os.path.join(WRITINGS_DIR, safe_name)

    try:
        if append and os.path.exists(filepath):
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"\n\n---\n*Continued {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
                f.write(content)
        else:
            # Add metadata header for new documents
            header = (
                f"---\n"
                f"title: {filename.replace('.md', '')}\n"
                f"author: Aeynis\n"
                f"date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"---\n\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header + content)

        size = os.path.getsize(filepath)
        return {
            "success": True,
            "filename": safe_name,
            "path": filepath,
            "size_bytes": size,
            "action": "appended" if append else "created",
        }
    except OSError as e:
        return {"success": False, "error": str(e)}


def read_document_from_disk(filename: str) -> dict:
    """Read a document from the writings directory."""
    filepath = os.path.join(WRITINGS_DIR, filename)

    # Try exact match first, then fuzzy
    if not os.path.isfile(filepath):
        # Search for partial match
        for entry in os.scandir(WRITINGS_DIR):
            if entry.is_file() and filename.lower() in entry.name.lower():
                filepath = entry.path
                break
        else:
            return {"success": False, "error": f"Document not found: {filename}"}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return {
            "success": True,
            "filename": os.path.basename(filepath),
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
        }
    except OSError as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Calendar Operations
# ---------------------------------------------------------------------------
def calendar_add(title: str, date: str, time: str = "", description: str = "") -> dict:
    """Add an event to the calendar."""
    # Parse date
    parsed = _parse_date(date)
    if not parsed:
        return {"success": False, "error": f"Could not parse date: {date}"}

    # Load existing events
    events = []
    if os.path.exists(CALENDAR_FILE):
        try:
            with open(CALENDAR_FILE, "r") as f:
                events = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    event_id = f"evt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{len(events)}"
    event = {
        "id": event_id,
        "title": title,
        "date": parsed.strftime("%Y-%m-%d"),
        "time": time,
        "description": description,
        "created_at": datetime.now().isoformat(),
    }

    events.append(event)

    # Save
    try:
        tmp = CALENDAR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f, indent=2)
        os.replace(tmp, CALENDAR_FILE)
        return {"success": True, "event_id": event_id, "event": event}
    except OSError as e:
        return {"success": False, "error": str(e)}


def calendar_list(days_ahead: int = 7) -> dict:
    """List upcoming calendar events."""
    events = []
    if os.path.exists(CALENDAR_FILE):
        try:
            with open(CALENDAR_FILE, "r") as f:
                events = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    today = datetime.now().strftime("%Y-%m-%d")
    future = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    upcoming = [e for e in events if today <= e.get("date", "") <= future]
    upcoming.sort(key=lambda e: e.get("date", ""))

    return {
        "success": True,
        "events": upcoming,
        "total_events": len(events),
        "days_ahead": days_ahead,
    }


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse a date string in various formats."""
    date_str = date_str.strip().lower()
    now = datetime.now()

    if date_str == "today":
        return now
    if date_str == "tomorrow":
        return now + timedelta(days=1)
    if date_str == "yesterday":
        return now - timedelta(days=1)

    day_names = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]
    for i, name in enumerate(day_names):
        if name in date_str:
            current_day = now.weekday()
            if "last" in date_str:
                delta = (current_day - i) % 7 or 7
                return now - timedelta(days=delta)
            elif "next" in date_str:
                delta = (i - current_day) % 7 or 7
                return now + timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%B %d %Y",
                "%b %d, %Y", "%b %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------
def build_server(test_mode: bool = False) -> Server:
    """Build the MCP server with all tools and resources."""

    server = Server("aeynis-bridge")
    state = BridgeState()

    # ── Tools ──────────────────────────────────────────────────

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="get_time",
                description="Get the current system date and time",
                inputSchema={"type": "object", "properties": {}, "required": []},
            ),
        ]

        if not test_mode:
            tools.extend([
                Tool(
                    name="write_document",
                    description="Create or edit a document. Content is saved to Aeynis's writings folder.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Name for the document (e.g. 'Bridges of Imagination')",
                            },
                            "content": {
                                "type": "string",
                                "description": "The text content to write",
                            },
                            "append": {
                                "type": "boolean",
                                "description": "If true, append to existing document instead of creating new",
                                "default": False,
                            },
                        },
                        "required": ["filename", "content"],
                    },
                ),
                Tool(
                    name="read_document",
                    description="Read the contents of one of Aeynis's documents",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Name or partial name of the document to read",
                            },
                        },
                        "required": ["filename"],
                    },
                ),
                Tool(
                    name="list_documents",
                    description="List all documents Aeynis has written",
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="calendar_add_event",
                    description="Add an event to Aeynis's calendar",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Event title (e.g. 'Story session with Jim')",
                            },
                            "date": {
                                "type": "string",
                                "description": "Event date (e.g. 'tomorrow', '2026-04-01', 'next Friday')",
                            },
                            "time": {
                                "type": "string",
                                "description": "Event time (e.g. '2:00 PM'). Optional.",
                                "default": "",
                            },
                            "description": {
                                "type": "string",
                                "description": "Additional details about the event. Optional.",
                                "default": "",
                            },
                        },
                        "required": ["title", "date"],
                    },
                ),
                Tool(
                    name="calendar_list_events",
                    description="List upcoming events from Aeynis's calendar",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "days_ahead": {
                                "type": "integer",
                                "description": "Number of days ahead to show (default 7)",
                                "default": 7,
                            },
                        },
                        "required": [],
                    },
                ),
            ])

        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "get_time":
                now = datetime.now()
                return [TextContent(
                    type="text",
                    text=f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}",
                )]

            elif name == "write_document":
                result = write_document_to_disk(
                    filename=arguments.get("filename", ""),
                    content=arguments.get("content", ""),
                    append=arguments.get("append", False),
                )
                state.refresh_documents()
                if result["success"]:
                    return [TextContent(
                        type="text",
                        text=f"Document {result['action']}: {result['filename']} ({result['size_bytes']} bytes)",
                    )]
                return [TextContent(type="text", text=f"Error: {result['error']}")]

            elif name == "read_document":
                result = read_document_from_disk(arguments.get("filename", ""))
                if result["success"]:
                    return [TextContent(
                        type="text",
                        text=f"=== {result['filename']} ===\n{result['content']}",
                    )]
                return [TextContent(type="text", text=f"Error: {result['error']}")]

            elif name == "list_documents":
                docs = state.refresh_documents()
                if not docs:
                    return [TextContent(type="text", text="No documents found in writings folder.")]
                lines = []
                for d in docs:
                    lines.append(f"- {d['filename']} (modified: {d['last_modified']}, {d['size_bytes']} bytes)")
                return [TextContent(
                    type="text",
                    text=f"Your documents ({len(docs)} files):\n" + "\n".join(lines),
                )]

            elif name == "calendar_add_event":
                result = calendar_add(
                    title=arguments.get("title", ""),
                    date=arguments.get("date", ""),
                    time=arguments.get("time", ""),
                    description=arguments.get("description", ""),
                )
                state.refresh_calendar()
                if result["success"]:
                    evt = result["event"]
                    return [TextContent(
                        type="text",
                        text=f"Event added: '{evt['title']}' on {evt['date']}" +
                             (f" at {evt['time']}" if evt.get('time') else ""),
                    )]
                return [TextContent(type="text", text=f"Error: {result['error']}")]

            elif name == "calendar_list_events":
                result = calendar_list(days_ahead=arguments.get("days_ahead", 7))
                state.refresh_calendar()
                if not result["events"]:
                    return [TextContent(
                        type="text",
                        text=f"No events in the next {result['days_ahead']} days. ({result['total_events']} total events on calendar.)",
                    )]
                lines = []
                for e in result["events"]:
                    time_str = f" at {e['time']}" if e.get("time") else ""
                    desc = f" — {e['description']}" if e.get("description") else ""
                    lines.append(f"- {e['date']}{time_str}: {e['title']}{desc}")
                return [TextContent(
                    type="text",
                    text=f"Upcoming events ({result['days_ahead']} days):\n" + "\n".join(lines),
                )]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        except Exception as e:
            logger.error(f"Tool '{name}' error: {e}")
            return [TextContent(type="text", text=f"Error in {name}: {str(e)}")]

    # ── Resources ──────────────────────────────────────────────

    if not test_mode:
        @server.list_resources()
        async def list_resources() -> list[Resource]:
            return [
                Resource(
                    uri="bridge://writing_index",
                    name="Writing Index",
                    description="Persistent list of all documents Aeynis has written",
                    mimeType="application/json",
                ),
                Resource(
                    uri="bridge://calendar_summary",
                    name="Calendar Summary",
                    description="Upcoming events overview",
                    mimeType="application/json",
                ),
            ]

        @server.read_resource()
        async def read_resource(uri: str) -> str:
            if uri == "bridge://writing_index":
                docs = state.refresh_documents()
                return json.dumps(docs, indent=2, default=str)
            elif uri == "bridge://calendar_summary":
                events = state.refresh_calendar()
                return json.dumps(events, indent=2, default=str)
            else:
                return json.dumps({"error": f"Unknown resource: {uri}"})

    return server


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(test_mode: bool = False):
    server = build_server(test_mode=test_mode)
    mode = "TEST" if test_mode else "FULL"
    logger.info(f"Starting Aeynis Bridge MCP Server ({mode} mode)")
    logger.info(f"Writings: {WRITINGS_DIR}")
    logger.info(f"Calendar: {CALENDAR_FILE}")
    logger.info(f"State: {STATE_FILE}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Aeynis Bridge MCP Server")
    parser.add_argument("--test", action="store_true",
                        help="Run minimal test server (get_time tool only)")
    args = parser.parse_args()

    asyncio.run(main(test_mode=args.test))
