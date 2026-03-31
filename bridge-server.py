#!/usr/bin/env python3
"""
Aeynis Bridge MCP Server - Structured tool access for Aeynis

A Model Context Protocol (MCP) server that wraps Aeynis's existing tools
(writing, calendar) with proper JSON schemas and structured responses.
Runs on STDIO transport — no HTTP required.

Uses FastMCP (recommended high-level API) — type hints auto-generate
JSON schemas, return values auto-serialize.

Usage:
  python bridge-server.py              # Full server with all tools
  python bridge-server.py --test       # Minimal test server (get_time only)

KoboldCpp integration:
  ./koboldcpp --model mistral-nemo.gguf --mcp_server stdio:"python bridge-server.py"

Tools:
  write_document     - Create/edit documents via AbiWord or direct file I/O
  read_document      - View document contents
  list_documents     - Show all documents Aeynis has created
  calendar_add_event - Create a calendar event
  calendar_list      - Query upcoming events
  get_time           - Get current system time (always available)

Resources:
  bridge://writing_index    - Persistent list of all documents
  bridge://calendar_summary - Upcoming events overview

State: ~/.aeynis-state/state.json (persists across KoboldCpp restarts)
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

# MCP SDK imports
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: MCP SDK not installed. Run: pip install 'mcp[cli]'", file=sys.stderr)
    sys.exit(1)

# Logging must go to stderr — stdout is the MCP protocol stream
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    stream=sys.stderr)
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

for d in [WRITINGS_DIR, CALENDAR_DIR, STATE_DIR]:
    os.makedirs(d, exist_ok=True)

# ---------------------------------------------------------------------------
# Persistent State
# ---------------------------------------------------------------------------
class BridgeState:
    """Persistent state that survives KoboldCpp restarts."""

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

    def refresh_documents(self) -> list:
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

    def refresh_calendar(self) -> list:
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


# Singleton state
_state = BridgeState()

# ---------------------------------------------------------------------------
# Date Parsing Helper
# ---------------------------------------------------------------------------
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
mcp = FastMCP("aeynis-bridge")


# ── Always-on tool ─────────────────────────────────────────

@mcp.tool()
def get_time() -> str:
    """Get the current system date and time"""
    now = datetime.now()
    return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"


# ── Document tools ─────────────────────────────────────────

@mcp.tool()
def write_document(filename: str, content: str, append: bool = False) -> str:
    """Create or edit a document. Content is saved to Aeynis's writings folder.

    Args:
        filename: Name for the document (e.g. 'Bridges of Imagination')
        content: The text content to write
        append: If true, append to existing document instead of creating new
    """
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
            action = "appended"
        else:
            header = (
                f"---\n"
                f"title: {filename.replace('.md', '')}\n"
                f"author: Aeynis\n"
                f"date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"---\n\n"
            )
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header + content)
            action = "created"

        size = os.path.getsize(filepath)
        _state.refresh_documents()
        return f"Document {action}: {safe_name} ({size} bytes)"

    except OSError as e:
        return f"Error writing document: {e}"


@mcp.tool()
def read_document(filename: str) -> str:
    """Read the contents of one of Aeynis's documents.

    Args:
        filename: Name or partial name of the document to read
    """
    filepath = os.path.join(WRITINGS_DIR, filename)

    # Try exact match first, then fuzzy
    if not os.path.isfile(filepath):
        for entry in os.scandir(WRITINGS_DIR):
            if entry.is_file() and filename.lower() in entry.name.lower():
                filepath = entry.path
                break
        else:
            return f"Document not found: {filename}"

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return f"=== {os.path.basename(filepath)} ===\n{content}"
    except OSError as e:
        return f"Error reading document: {e}"


@mcp.tool()
def list_documents() -> str:
    """List all documents Aeynis has written"""
    docs = _state.refresh_documents()
    if not docs:
        return "No documents found in writings folder."
    lines = []
    for d in docs:
        lines.append(f"- {d['filename']} (modified: {d['last_modified']}, {d['size_bytes']} bytes)")
    return f"Your documents ({len(docs)} files):\n" + "\n".join(lines)


# ── Calendar tools ─────────────────────────────────────────

@mcp.tool()
def calendar_add_event(title: str, date: str, time: str = "", description: str = "") -> str:
    """Add an event to Aeynis's calendar.

    Args:
        title: Event title (e.g. 'Story session with Jim')
        date: Event date (e.g. 'tomorrow', '2026-04-01', 'next Friday')
        time: Event time (e.g. '2:00 PM'). Optional.
        description: Additional details about the event. Optional.
    """
    parsed = _parse_date(date)
    if not parsed:
        return f"Could not parse date: '{date}'"

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

    try:
        tmp = CALENDAR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(events, f, indent=2)
        os.replace(tmp, CALENDAR_FILE)
        _state.refresh_calendar()
        time_str = f" at {time}" if time else ""
        return f"Event added: '{title}' on {event['date']}{time_str}"
    except OSError as e:
        return f"Error saving event: {e}"


@mcp.tool()
def calendar_list_events(days_ahead: int = 7) -> str:
    """List upcoming events from Aeynis's calendar.

    Args:
        days_ahead: Number of days ahead to show (default 7)
    """
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

    _state.refresh_calendar()

    if not upcoming:
        return f"No events in the next {days_ahead} days. ({len(events)} total events on calendar.)"

    lines = []
    for e in upcoming:
        time_str = f" at {e['time']}" if e.get("time") else ""
        desc = f" — {e['description']}" if e.get("description") else ""
        lines.append(f"- {e['date']}{time_str}: {e['title']}{desc}")
    return f"Upcoming events ({days_ahead} days):\n" + "\n".join(lines)


# ── Resources ──────────────────────────────────────────────

@mcp.resource("bridge://writing_index")
def writing_index() -> str:
    """Persistent list of all documents Aeynis has written"""
    docs = _state.refresh_documents()
    return json.dumps(docs, indent=2, default=str)


@mcp.resource("bridge://calendar_summary")
def calendar_summary() -> str:
    """Upcoming events overview"""
    events = _state.refresh_calendar()
    return json.dumps(events, indent=2, default=str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Aeynis Bridge MCP Server")
    parser.add_argument("--test", action="store_true",
                        help="Run minimal test server (get_time tool only)")
    args = parser.parse_args()

    if args.test:
        # Minimal test server — only get_time
        test_mcp = FastMCP("aeynis-bridge-test")

        @test_mcp.tool()
        def get_time_test() -> str:
            """Get the current system date and time"""
            now = datetime.now()
            return f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"

        logger.info("Starting Aeynis Bridge MCP Server (TEST mode — get_time only)")
        test_mcp.run(transport="stdio")
    else:
        logger.info("Starting Aeynis Bridge MCP Server (FULL mode)")
        logger.info(f"Writings: {WRITINGS_DIR}")
        logger.info(f"Calendar: {CALENDAR_FILE}")
        logger.info(f"State: {STATE_FILE}")
        mcp.run(transport="stdio")
