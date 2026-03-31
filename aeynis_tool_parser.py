#!/usr/bin/env python3
"""
Aeynis Tool Parser - Parse structured tool tags from Aeynis's responses

Extracts tool invocations from Aeynis's response text. Tags are stripped
from the displayed response and returned as structured actions for the
backend to execute.

Tag format (kept simple for a 12B model):
  [WRITE: "Title of piece"]
  [WRITE: "Title" | content follows]
  [CALENDAR: "Event title" on "date"]
  [CALENDAR: "Event title" on "date" | recurring yearly]
  [MY_WRITINGS]
  [MY_CALENDAR]
  [EXPORT: "Title" as pdf]

Tags are case-insensitive and forgiving — partial matches are accepted.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_tool_tags(response: str) -> Tuple[str, List[Dict]]:
    """Parse tool tags from Aeynis's response.

    Args:
        response: The raw response text from the model

    Returns:
        (cleaned_response, actions) where:
          - cleaned_response has tool tags stripped out
          - actions is a list of dicts with 'tool' and tool-specific fields
    """
    actions = []
    cleaned = response

    # ── [WRITE: "title"] or [WRITE: "title" | content follows] ──
    write_patterns = [
        r'\[WRITE:\s*"([^"]+)"(?:\s*\|\s*(.+?))?\]',
        r'\[WRITE:\s*([^\]|]+?)(?:\s*\|\s*(.+?))?\]',
    ]
    for pattern in write_patterns:
        for m in re.finditer(pattern, cleaned, re.IGNORECASE):
            title = m.group(1).strip().strip('"\'')
            note = m.group(2).strip() if m.group(2) else ""
            actions.append({
                "tool": "write",
                "title": title,
                "note": note,
            })
            logger.info(f"Parsed WRITE tag: title='{title}'")
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    # ── [CALENDAR: "event" on "date"] ──
    cal_patterns = [
        r'\[CALENDAR:\s*"([^"]+)"\s+on\s+"([^"]+)"(?:\s*\|\s*(.+?))?\]',
        r'\[CALENDAR:\s*([^\]|]+?)\s+on\s+([^\]|]+?)(?:\s*\|\s*(.+?))?\]',
        r'\[CALENDAR:\s*"([^"]+)"(?:\s*\|\s*(.+?))?\]',
    ]
    for pattern in cal_patterns:
        for m in re.finditer(pattern, cleaned, re.IGNORECASE):
            groups = m.groups()
            if len(groups) >= 2 and groups[1]:
                title = groups[0].strip().strip('"\'')
                date = groups[1].strip().strip('"\'')
                extra = groups[2].strip() if len(groups) > 2 and groups[2] else ""
                actions.append({
                    "tool": "calendar_add",
                    "title": title,
                    "date": date,
                    "extra": extra,
                })
                logger.info(f"Parsed CALENDAR tag: '{title}' on '{date}'")
            else:
                title = groups[0].strip().strip('"\'')
                actions.append({
                    "tool": "calendar_query",
                    "query": title,
                })
                logger.info(f"Parsed CALENDAR query tag: '{title}'")
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    # ── [MY_WRITINGS] or [SHOW_WRITINGS] ──
    writings_pattern = r'\[(?:MY_WRITINGS|SHOW_WRITINGS|LIST_WRITINGS)\]'
    if re.search(writings_pattern, cleaned, re.IGNORECASE):
        actions.append({"tool": "list_writings"})
        logger.info("Parsed MY_WRITINGS tag")
    cleaned = re.sub(writings_pattern, '', cleaned, flags=re.IGNORECASE)

    # ── [MY_CALENDAR] or [SHOW_CALENDAR] ──
    calendar_pattern = r'\[(?:MY_CALENDAR|SHOW_CALENDAR|LIST_CALENDAR)\]'
    if re.search(calendar_pattern, cleaned, re.IGNORECASE):
        actions.append({"tool": "list_calendar"})
        logger.info("Parsed MY_CALENDAR tag")
    cleaned = re.sub(calendar_pattern, '', cleaned, flags=re.IGNORECASE)

    # ── [EXPORT: "title" as format] ──
    export_pattern = r'\[EXPORT:\s*"?([^"|\]]+)"?\s+as\s+(\w+)\]'
    for m in re.finditer(export_pattern, cleaned, re.IGNORECASE):
        title = m.group(1).strip().strip('"\'')
        fmt = m.group(2).strip().lower()
        actions.append({
            "tool": "export",
            "title": title,
            "format": fmt,
        })
        logger.info(f"Parsed EXPORT tag: '{title}' as {fmt}")
    cleaned = re.sub(export_pattern, '', cleaned, flags=re.IGNORECASE)

    # ── [READ_WRITING: "title"] ──
    read_pattern = r'\[READ_WRITING:\s*"?([^"|\]]+)"?\]'
    for m in re.finditer(read_pattern, cleaned, re.IGNORECASE):
        title = m.group(1).strip().strip('"\'')
        actions.append({
            "tool": "read_writing",
            "title": title,
        })
        logger.info(f"Parsed READ_WRITING tag: '{title}'")
    cleaned = re.sub(read_pattern, '', cleaned, flags=re.IGNORECASE)

    # ── JSON function call blocks ──
    # The model sometimes outputs tool calls as JSON blocks like:
    #   ```json
    #   {"function": "calendar_list_events", "arguments": {"days_ahead": 7}}
    #   ```
    # Parse these and convert to actions.
    json_block_pattern = r'```json\s*\n?\s*(\{[^`]+?\})\s*\n?\s*```'
    for m in re.finditer(json_block_pattern, cleaned, re.DOTALL):
        try:
            import json
            call = json.loads(m.group(1))
            func_name = call.get("function", call.get("name", ""))
            args = call.get("arguments", call.get("params", {}))

            if func_name == "calendar_list_events":
                actions.append({"tool": "list_calendar"})
                logger.info(f"Parsed JSON function call: {func_name}")
            elif func_name == "calendar_add_event":
                actions.append({
                    "tool": "calendar_add",
                    "title": args.get("title", ""),
                    "date": args.get("date", ""),
                    "extra": args.get("description", ""),
                })
                logger.info(f"Parsed JSON function call: {func_name}")
            elif func_name == "read_document":
                actions.append({
                    "tool": "read_writing",
                    "title": args.get("filename", ""),
                })
                logger.info(f"Parsed JSON function call: {func_name}")
            elif func_name == "write_document":
                actions.append({
                    "tool": "write",
                    "title": args.get("filename", ""),
                    "note": "",
                })
                logger.info(f"Parsed JSON function call: {func_name}")
            elif func_name == "list_documents":
                actions.append({"tool": "list_writings"})
                logger.info(f"Parsed JSON function call: {func_name}")
            elif func_name == "get_time":
                logger.info(f"Parsed JSON function call: {func_name} (no-op)")
            else:
                logger.info(f"Unknown JSON function call: {func_name}")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"Failed to parse JSON function call: {e}")
    cleaned = re.sub(json_block_pattern, '', cleaned, flags=re.DOTALL)

    # Also catch inline JSON (without code fences)
    inline_json_pattern = r'\{\s*"function"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{[^}]*\})\s*\}'
    for m in re.finditer(inline_json_pattern, cleaned):
        try:
            import json
            func_name = m.group(1)
            args = json.loads(m.group(2))

            if func_name == "calendar_list_events":
                actions.append({"tool": "list_calendar"})
            elif func_name == "read_document":
                actions.append({"tool": "read_writing", "title": args.get("filename", "")})
            elif func_name == "list_documents":
                actions.append({"tool": "list_writings"})
            elif func_name == "calendar_add_event":
                actions.append({"tool": "calendar_add", "title": args.get("title", ""), "date": args.get("date", "")})
            logger.info(f"Parsed inline JSON function call: {func_name}")
        except (json.JSONDecodeError, AttributeError):
            pass
    cleaned = re.sub(inline_json_pattern, '', cleaned)

    # Clean up whitespace artifacts from tag removal
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = cleaned.strip()

    if actions:
        logger.info(f"Parsed {len(actions)} tool action(s) from response")

    return cleaned, actions
