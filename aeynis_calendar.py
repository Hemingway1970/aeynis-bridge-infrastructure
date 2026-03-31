#!/usr/bin/env python3
"""
Aeynis Calendar - Persistent event tracking for Aeynis

A lightweight JSON-based calendar that lets Aeynis track dates, events,
milestones, and temporal connections. Events persist across sessions.

Storage: ~/AeynisLibrary/calendar/events.json
No VRAM impact - runs on system RAM/CPU only.

Events can optionally link to writings or library files, giving Aeynis
the ability to build her own map of time and experience.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

CALENDAR_SUBDIR = "calendar"
EVENTS_FILE = "events.json"


class AeynisCalendar:
    """
    Manages Aeynis's personal calendar.

    Capabilities:
      - add_event()        : Create a new event/milestone
      - list_events()      : List events in a date range
      - query_events()     : Search events by keyword
      - get_event()        : Get a specific event by ID
      - update_event()     : Modify an existing event
      - delete_event()     : Remove an event
      - upcoming()         : Get events in the next N days
      - on_this_day()      : Events that happened on a specific date
      - recent()           : Events from the last N days
    """

    def __init__(self, library_root: str):
        self.calendar_dir = os.path.join(library_root, CALENDAR_SUBDIR)
        self.events_file = os.path.join(self.calendar_dir, EVENTS_FILE)
        os.makedirs(self.calendar_dir, exist_ok=True)

        # Load existing events or create empty store
        self._events = self._load_events()
        logger.info(f"AeynisCalendar initialized with {len(self._events)} events")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_event(self, title: str, date: str,
                  description: str = "",
                  tags: Optional[List[str]] = None,
                  linked_file: str = "",
                  recurring: str = "") -> Dict:
        """Add a new event.

        Args:
            title:       Event title
            date:        Date string (YYYY-MM-DD or YYYY-MM-DD HH:MM)
            description: Optional longer description
            tags:        Optional tags (e.g., ["milestone", "birthday", "reading"])
            linked_file: Optional path to a linked writing or library file
            recurring:   Optional recurrence ("yearly", "monthly", "weekly", or "")

        Returns dict with success, event_id, event.
        """
        parsed_date = self._parse_date(date)
        if not parsed_date:
            return {"error": f"Could not parse date: '{date}'", "success": False}

        event_id = f"evt_{datetime.now().strftime('%Y%m%d%H%M%S')}_{len(self._events)}"

        event = {
            "id": event_id,
            "title": title,
            "date": parsed_date.strftime("%Y-%m-%d"),
            "time": parsed_date.strftime("%H:%M") if parsed_date.hour or parsed_date.minute else "",
            "description": description,
            "tags": tags or [],
            "linked_file": linked_file,
            "recurring": recurring,
            "created_at": datetime.now().isoformat(),
        }

        self._events.append(event)
        self._save_events()
        logger.info(f"Added calendar event: '{title}' on {event['date']}")

        return {"success": True, "event_id": event_id, "event": event}

    def list_events(self, start_date: str = "", end_date: str = "") -> List[Dict]:
        """List events, optionally filtered to a date range.

        Args:
            start_date: Start of range (YYYY-MM-DD), default = beginning of time
            end_date:   End of range (YYYY-MM-DD), default = end of time

        Returns list of events sorted by date.
        """
        events = list(self._events)

        if start_date:
            start = self._parse_date(start_date)
            if start:
                start_str = start.strftime("%Y-%m-%d")
                events = [e for e in events if e["date"] >= start_str]

        if end_date:
            end = self._parse_date(end_date)
            if end:
                end_str = end.strftime("%Y-%m-%d")
                events = [e for e in events if e["date"] <= end_str]

        events.sort(key=lambda e: e["date"])
        return events

    def query_events(self, query: str) -> List[Dict]:
        """Search events by keyword in title, description, or tags."""
        query_lower = query.lower()
        results = []

        for event in self._events:
            if (query_lower in event["title"].lower()
                    or query_lower in event.get("description", "").lower()
                    or any(query_lower in t.lower() for t in event.get("tags", []))):
                results.append(event)

        results.sort(key=lambda e: e["date"], reverse=True)
        return results

    def get_event(self, event_id: str) -> Dict:
        """Get a specific event by ID."""
        for event in self._events:
            if event["id"] == event_id:
                return {"success": True, "event": event}
        return {"error": f"Event not found: {event_id}", "success": False}

    def update_event(self, event_id: str, **updates) -> Dict:
        """Update an existing event.

        Accepted fields: title, date, description, tags, linked_file, recurring.
        """
        for i, event in enumerate(self._events):
            if event["id"] == event_id:
                for key in ("title", "description", "tags", "linked_file", "recurring"):
                    if key in updates:
                        self._events[i][key] = updates[key]

                if "date" in updates:
                    parsed = self._parse_date(updates["date"])
                    if parsed:
                        self._events[i]["date"] = parsed.strftime("%Y-%m-%d")
                        self._events[i]["time"] = parsed.strftime("%H:%M") if parsed.hour or parsed.minute else ""

                self._events[i]["updated_at"] = datetime.now().isoformat()
                self._save_events()
                logger.info(f"Updated event {event_id}")
                return {"success": True, "event": self._events[i]}

        return {"error": f"Event not found: {event_id}", "success": False}

    def delete_event(self, event_id: str) -> Dict:
        """Delete an event by ID."""
        for i, event in enumerate(self._events):
            if event["id"] == event_id:
                removed = self._events.pop(i)
                self._save_events()
                logger.info(f"Deleted event: '{removed['title']}'")
                return {"success": True, "deleted": removed["title"]}
        return {"error": f"Event not found: {event_id}", "success": False}

    def upcoming(self, days: int = 7) -> List[Dict]:
        """Get events in the next N days."""
        today = datetime.now().strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        return self.list_events(start_date=today, end_date=future)

    def recent(self, days: int = 7) -> List[Dict]:
        """Get events from the last N days."""
        past = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        return self.list_events(start_date=past, end_date=today)

    def on_this_day(self, date: str = "") -> List[Dict]:
        """Get events that fall on a specific date (or today).

        Also returns recurring events that match (e.g., yearly birthdays).
        """
        if not date:
            target = datetime.now()
        else:
            target = self._parse_date(date)
            if not target:
                return []

        target_str = target.strftime("%Y-%m-%d")
        target_md = target.strftime("%m-%d")  # For yearly recurring

        results = []
        for event in self._events:
            # Exact date match
            if event["date"] == target_str:
                results.append(event)
                continue

            # Recurring yearly: same month-day
            if event.get("recurring") == "yearly":
                if event["date"][5:] == target_md:
                    results.append(event)
                    continue

            # Recurring monthly: same day of month
            if event.get("recurring") == "monthly":
                if event["date"][8:] == target_str[8:]:
                    results.append(event)
                    continue

        return results

    def format_for_context(self) -> str:
        """Format calendar summary for injection into system prompt.

        Shows today's events, upcoming events, and recent milestones.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_events = self.on_this_day()
        upcoming_events = self.upcoming(days=7)
        # Remove today's events from upcoming to avoid duplicates
        upcoming_events = [e for e in upcoming_events if e["date"] != today_str]

        if not today_events and not upcoming_events and not self._events:
            return ""

        lines = []
        lines.append(f"\nYOUR CALENDAR (today is {datetime.now().strftime('%A, %B %d, %Y')}):")

        if today_events:
            lines.append("  Today:")
            for e in today_events:
                time_str = f" at {e['time']}" if e.get("time") else ""
                lines.append(f"    - {e['title']}{time_str}")

        if upcoming_events:
            lines.append("  Coming up:")
            for e in upcoming_events[:5]:
                lines.append(f"    - {e['date']}: {e['title']}")

        total = len(self._events)
        if total > 0:
            lines.append(f"  ({total} total events tracked)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_events(self) -> List[Dict]:
        """Load events from the JSON file."""
        if not os.path.exists(self.events_file):
            return []
        try:
            with open(self.events_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load calendar events: {e}")
            return []

    def _save_events(self):
        """Save events to the JSON file atomically."""
        try:
            tmp_path = self.events_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._events, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.events_file)
        except OSError as e:
            logger.error(f"Failed to save calendar events: {e}")

    @staticmethod
    def _parse_date(date_str: str) -> Optional[datetime]:
        """Parse a date string in various formats.

        Handles combined date+time like 'tomorrow 1:00 PM', 'next Friday 3pm'.
        """
        date_str = date_str.strip()

        # Handle relative dates
        lower = date_str.lower()
        now = datetime.now()

        # Extract time component if present (e.g., "tomorrow 1:00 PM" → "tomorrow" + "1:00 PM")
        time_part = None
        time_match = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:am|pm|AM|PM))', date_str)
        if not time_match:
            # Try 24h format like "14:00"
            time_match = re.search(r'(\d{1,2}:\d{2})(?!\d)', date_str)
        if time_match:
            time_str = time_match.group(1).strip()
            # Parse the time
            for tfmt in ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M"):
                try:
                    time_part = datetime.strptime(time_str, tfmt)
                    break
                except ValueError:
                    continue
            # Remove the time from the date string for date-only parsing
            lower = date_str[:time_match.start()].strip().lower()
            if not lower:
                lower = "today"  # Just a time with no date means today

        def _apply_time(dt: datetime) -> datetime:
            if time_part:
                return dt.replace(hour=time_part.hour, minute=time_part.minute, second=0)
            return dt

        if lower == "today":
            return _apply_time(now)
        if lower == "tomorrow":
            return _apply_time(now + timedelta(days=1))
        if lower == "yesterday":
            return _apply_time(now - timedelta(days=1))

        # "last tuesday", "next friday", etc.
        day_names = ["monday", "tuesday", "wednesday", "thursday",
                     "friday", "saturday", "sunday"]
        for i, name in enumerate(day_names):
            if name in lower:
                current_day = now.weekday()
                if "last" in lower:
                    delta = (current_day - i) % 7
                    if delta == 0:
                        delta = 7
                    return _apply_time(now - timedelta(days=delta))
                elif "next" in lower:
                    delta = (i - current_day) % 7
                    if delta == 0:
                        delta = 7
                    return _apply_time(now + timedelta(days=delta))
                else:
                    # Closest occurrence (past or future)
                    delta = (i - current_day) % 7
                    return _apply_time(now + timedelta(days=delta))

        # Standard formats
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %H:%M",
                    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                    "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        return None
