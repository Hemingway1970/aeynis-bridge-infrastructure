#!/usr/bin/env python3
"""
Memory Consolidation Service for Aeynis

Automatically groups related memory fragments into consolidated summaries.
Runs after conversations or on a schedule to keep memories searchable.

The problem: memories are stored as individual "Jim said:" / "Aeynis responded:"
fragments. A 44-part story becomes 44 tiny memories that semantic search can only
partially retrieve. This service consolidates them into coherent summaries.

Usage:
    python3 memory_consolidator.py              # Run once
    python3 memory_consolidator.py --watch      # Run continuously (every 30 min)
    python3 memory_consolidator.py --dry-run    # Show what would be consolidated
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import List, Dict, Tuple

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

MCP_MEMORY_URL = "http://localhost:8000"
KOBOLD_URL = "http://localhost:5001"
MEMORY_DB = os.path.expanduser("~/.local/share/mcp-memory/sqlite_vec.db")

# Consolidation settings
SESSION_GAP_SECONDS = 600      # 10 min gap = new conversation session
MIN_SESSION_MEMORIES = 4       # Don't consolidate tiny sessions
MAX_SUMMARY_CHARS = 800        # Max chars for a consolidated summary
CONSOLIDATION_TAG = "consolidated"


def get_all_memories() -> List[Dict]:
    """Fetch all memories directly from SQLite for complete access.

    The HTTP API paginates at 100 max, but SQLite has all memories.
    """
    if not os.path.exists(MEMORY_DB):
        logger.error(f"Memory database not found at {MEMORY_DB}")
        return []

    try:
        conn = sqlite3.connect(MEMORY_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT content, content_hash, created_at FROM memories ORDER BY created_at ASC")
        rows = cursor.fetchall()
        conn.close()

        memories = []
        for row in rows:
            content = row['content']
            # Detect consolidated memories by prefix
            tags = [CONSOLIDATION_TAG] if content.startswith("[Consolidated memory") else []
            memories.append({
                'content': content,
                'content_hash': row['content_hash'],
                'created_at': row['created_at'],
                'tags': tags,
            })

        logger.info(f"Loaded {len(memories)} memories from SQLite")
        return memories

    except Exception as e:
        logger.error(f"Failed to read SQLite: {e}")
        return []


def group_into_sessions(memories: List[Dict]) -> List[List[Dict]]:
    """Group memories into conversation sessions based on timestamps.

    Memories within SESSION_GAP_SECONDS of each other are in the same session.
    """
    if not memories:
        return []

    # Sort by creation time
    sorted_mems = sorted(memories, key=lambda m: m.get('created_at', 0))

    sessions = []
    current_session = [sorted_mems[0]]

    for mem in sorted_mems[1:]:
        prev_time = current_session[-1].get('created_at', 0)
        curr_time = mem.get('created_at', 0)

        if curr_time - prev_time > SESSION_GAP_SECONDS:
            sessions.append(current_session)
            current_session = [mem]
        else:
            current_session.append(mem)

    if current_session:
        sessions.append(current_session)

    return sessions


def is_already_consolidated(session: List[Dict]) -> bool:
    """Check if a session already has a consolidated summary"""
    for mem in session:
        tags = mem.get('tags', [])
        if CONSOLIDATION_TAG in tags:
            return True
    return False


def get_session_text(session: List[Dict]) -> str:
    """Extract readable text from a session's memories"""
    lines = []
    for mem in session:
        content = mem.get('content', '')
        tags = mem.get('tags', [])
        if CONSOLIDATION_TAG in tags:
            continue
        lines.append(content)
    return "\n".join(lines)


def summarize_with_kobold(session_text: str) -> str:
    """Use KoboldCpp to create a summary of a conversation session"""
    # Truncate input if too long for context
    if len(session_text) > 4000:
        session_text = session_text[:4000] + "\n[...truncated]"

    prompt = f"""### System:
You are a memory consolidation system. Read the following conversation and write a concise summary that captures:
1. The key topics discussed
2. Important facts, names, and details mentioned
3. Any stories, creative works, or emotional moments
4. Who said what (Jim is the human, Aeynis is the AI)

Write the summary in third person past tense. Include specific names, places, and details - these are important for future recall. Keep it under 500 words.

### Conversation:
{session_text}

### Summary:
"""

    try:
        response = requests.post(
            f"{KOBOLD_URL}/api/v1/generate",
            json={
                "prompt": prompt,
                "max_length": 400,
                "temperature": 0.3,  # Low temp for factual summary
                "top_p": 0.9,
                "rep_pen": 1.1,
                "stop_sequence": ["###", "\n\n\n"],
            },
            timeout=60,
        )
        if response.status_code == 200:
            result = response.json()
            summary = result['results'][0]['text'].strip()
            return summary
    except Exception as e:
        logger.error(f"KoboldCpp summarization failed: {e}")

    # Fallback: create a simple extractive summary
    return create_extractive_summary(session_text)


def create_extractive_summary(session_text: str) -> str:
    """Fallback summary when KoboldCpp is unavailable.

    Extracts key sentences rather than generating new text.
    """
    lines = session_text.split('\n')
    # Keep first line, last line, and any lines with names/key words
    key_lines = []
    seen = set()

    for line in lines:
        line = line.strip()
        if not line or line in seen:
            continue
        # Strip prefixes
        for prefix in ["Jim said: ", "Aeynis responded: "]:
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        # Keep lines with names, questions, or story elements
        if len(key_lines) < 3:  # Always keep first few
            key_lines.append(line[:200])
            seen.add(line)
        elif any(kw in line.lower() for kw in [
            'name', 'called', 'remember', 'story', 'dream',
            'important', 'love', 'thank', 'bridge', 'aeynis',
            '?',  # Questions are often important
        ]):
            key_lines.append(line[:200])
            seen.add(line)

        if len(key_lines) >= 8:
            break

    return " | ".join(key_lines)


def store_consolidated_memory(summary: str, session: List[Dict]) -> bool:
    """Store a consolidated memory back in the memory service"""
    timestamps = [m.get('created_at', 0) for m in session]
    earliest = min(timestamps) if timestamps else 0
    latest = max(timestamps) if timestamps else 0

    # Build descriptive tags
    tags = [CONSOLIDATION_TAG, "summary"]

    # Extract any character names or topics for better searchability
    text = summary.lower()
    for name in ['lyra', 'oliver', 'cesspanardo', 'cade', 'pat', 'fairy', 'fairies']:
        if name in text:
            tags.append(name)

    earliest_dt = datetime.fromtimestamp(earliest, tz=timezone.utc).strftime('%Y-%m-%d')
    content = f"[Consolidated memory from {earliest_dt}] {summary}"

    try:
        response = requests.post(
            f"{MCP_MEMORY_URL}/api/memories",
            json={
                "content": content,
                "tags": tags,
            },
            timeout=10,
        )
        if response.status_code in (200, 201):
            logger.info(f"Stored consolidated memory ({len(content)} chars, tags: {tags})")
            return True
        else:
            logger.error(f"Failed to store: {response.status_code} {response.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Failed to store consolidated memory: {e}")
        return False


def consolidate(dry_run=False):
    """Main consolidation routine"""
    logger.info("Starting memory consolidation...")

    memories = get_all_memories()
    if not memories:
        logger.info("No memories found")
        return 0

    # Filter out already-consolidated memories for grouping
    raw_memories = [m for m in memories if CONSOLIDATION_TAG not in m.get('tags', [])]
    logger.info(f"Found {len(memories)} total memories ({len(raw_memories)} raw, "
                f"{len(memories) - len(raw_memories)} consolidated)")

    sessions = group_into_sessions(raw_memories)
    logger.info(f"Grouped into {len(sessions)} conversation sessions")

    consolidated_count = 0

    for i, session in enumerate(sessions):
        if len(session) < MIN_SESSION_MEMORIES:
            continue

        # Check if this session's time range already has a consolidated memory
        session_times = [m.get('created_at', 0) for m in session]
        session_start = min(session_times)
        session_end = max(session_times)

        # Check existing consolidated memories for overlap
        already_done = False
        for mem in memories:
            if CONSOLIDATION_TAG in mem.get('tags', []):
                mem_time = mem.get('created_at', 0)
                if session_start <= mem_time <= session_end + 60:
                    already_done = True
                    break

        if already_done:
            continue

        session_text = get_session_text(session)
        if len(session_text) < 100:
            continue

        session_date = datetime.fromtimestamp(session_start, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        logger.info(f"\nSession {i+1}: {session_date} ({len(session)} memories, {len(session_text)} chars)")

        if dry_run:
            logger.info(f"  [DRY RUN] Would consolidate {len(session)} memories")
            logger.info(f"  Preview: {session_text[:200]}...")
            consolidated_count += 1
            continue

        # Generate summary
        logger.info("  Generating summary...")
        summary = summarize_with_kobold(session_text)
        if not summary:
            logger.warning("  Failed to generate summary, skipping")
            continue

        logger.info(f"  Summary ({len(summary)} chars): {summary[:150]}...")

        # Store it
        if store_consolidated_memory(summary, session):
            consolidated_count += 1

        # Small delay between consolidations
        time.sleep(1)

    logger.info(f"\nConsolidation complete. Created {consolidated_count} new summaries.")
    return consolidated_count


def watch_mode(interval_minutes=30):
    """Run consolidation on a schedule"""
    logger.info(f"Watch mode: consolidating every {interval_minutes} minutes")
    while True:
        try:
            consolidate()
        except Exception as e:
            logger.error(f"Consolidation error: {e}")
        logger.info(f"Sleeping {interval_minutes} minutes...")
        time.sleep(interval_minutes * 60)


def main():
    parser = argparse.ArgumentParser(description="Consolidate Aeynis memory fragments")
    parser.add_argument("--watch", action="store_true",
                        help="Run continuously every 30 minutes")
    parser.add_argument("--interval", type=int, default=30,
                        help="Minutes between consolidation runs (with --watch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be consolidated without doing it")
    args = parser.parse_args()

    if args.watch:
        watch_mode(args.interval)
    else:
        consolidate(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
