#!/usr/bin/env python3
"""
Aeynis Calendar API - Flask Blueprint

Exposes Aeynis's calendar as REST endpoints.

Endpoints:
  GET    /calendar/events              - List events (optional ?start=&end= range)
  GET    /calendar/events/<event_id>   - Get a specific event
  POST   /calendar/events              - Add a new event
  PUT    /calendar/events/<event_id>   - Update an event
  DELETE /calendar/events/<event_id>   - Delete an event
  GET    /calendar/upcoming            - Events in the next N days (?days=7)
  GET    /calendar/recent              - Events from the last N days (?days=7)
  GET    /calendar/today               - Today's events (including recurring)
  GET    /calendar/search?q=<query>    - Search events by keyword
"""

import logging
from flask import Blueprint, request, jsonify

from aeynis_calendar import AeynisCalendar

logger = logging.getLogger(__name__)

calendar_bp = Blueprint("calendar", __name__)

# Singleton instance
_calendar: AeynisCalendar = None


def init_calendar(library_root: str) -> AeynisCalendar:
    """Initialize the calendar singleton. Called from the main app."""
    global _calendar
    _calendar = AeynisCalendar(library_root)
    return _calendar


def get_calendar() -> AeynisCalendar:
    global _calendar
    if _calendar is None:
        raise RuntimeError("Calendar not initialized. Call init_calendar() first.")
    return _calendar


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@calendar_bp.route("/calendar/events", methods=["GET"])
def list_events():
    """List events, optionally filtered by date range."""
    cal = get_calendar()
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    events = cal.list_events(start_date=start, end_date=end)
    return jsonify({"events": events, "count": len(events)})


@calendar_bp.route("/calendar/events/<event_id>", methods=["GET"])
def get_event(event_id):
    """Get a specific event by ID."""
    cal = get_calendar()
    result = cal.get_event(event_id)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@calendar_bp.route("/calendar/events", methods=["POST"])
def add_event():
    """Add a new event.

    JSON body:
      title:       str (required)
      date:        str (required) - YYYY-MM-DD, relative ("tomorrow"), etc.
      description: str (optional)
      tags:        list[str] (optional)
      linked_file: str (optional)
      recurring:   str (optional) - "yearly", "monthly", "weekly", or ""
    """
    data = request.json or {}
    title = data.get("title")
    date = data.get("date")

    if not title or not date:
        return jsonify({"error": "title and date are required"}), 400

    cal = get_calendar()
    result = cal.add_event(
        title=title,
        date=date,
        description=data.get("description", ""),
        tags=data.get("tags"),
        linked_file=data.get("linked_file", ""),
        recurring=data.get("recurring", ""),
    )

    if not result.get("success"):
        return jsonify(result), 400
    return jsonify(result), 201


@calendar_bp.route("/calendar/events/<event_id>", methods=["PUT"])
def update_event(event_id):
    """Update an existing event.

    JSON body: any of title, date, description, tags, linked_file, recurring
    """
    data = request.json or {}
    cal = get_calendar()
    result = cal.update_event(event_id, **data)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@calendar_bp.route("/calendar/events/<event_id>", methods=["DELETE"])
def delete_event(event_id):
    """Delete an event."""
    cal = get_calendar()
    result = cal.delete_event(event_id)
    if not result.get("success"):
        return jsonify(result), 404
    return jsonify(result)


@calendar_bp.route("/calendar/upcoming", methods=["GET"])
def upcoming():
    """Get events in the next N days (default 7)."""
    days = request.args.get("days", 7, type=int)
    cal = get_calendar()
    events = cal.upcoming(days=days)
    return jsonify({"events": events, "count": len(events), "days": days})


@calendar_bp.route("/calendar/recent", methods=["GET"])
def recent():
    """Get events from the last N days (default 7)."""
    days = request.args.get("days", 7, type=int)
    cal = get_calendar()
    events = cal.recent(days=days)
    return jsonify({"events": events, "count": len(events), "days": days})


@calendar_bp.route("/calendar/today", methods=["GET"])
def today():
    """Get today's events (including recurring matches)."""
    cal = get_calendar()
    events = cal.on_this_day()
    return jsonify({"events": events, "count": len(events)})


@calendar_bp.route("/calendar/search", methods=["GET"])
def search_events():
    """Search events by keyword."""
    query = request.args.get("q", "")
    if not query:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    cal = get_calendar()
    results = cal.query_events(query)
    return jsonify({"query": query, "results": results, "count": len(results)})
