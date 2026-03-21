#!/usr/bin/env python3
"""
Ingest conversation transcripts into Aeynis memory as consolidated summaries.

Reads saved transcript files, splits them into logical chunks, and stores
each chunk as a consolidated memory with proper tags.

Usage:
    python3 ingest_transcripts.py ~/Downloads/aeynis_2026-03-14T*.txt
"""

import os
import re
import sys
import requests

MCP_MEMORY_URL = "http://localhost:8000"
CHUNK_SIZE = 2000  # chars per memory chunk — large enough for context, small enough to search


def parse_transcript(filepath: str) -> list:
    """Parse a transcript file into conversation turns."""
    with open(filepath, 'r') as f:
        content = f.read()

    # Extract date from filename or header
    basename = os.path.basename(filepath)
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', basename)
    date_str = date_match.group(1) if date_match else "unknown-date"

    # Split into turns
    turns = []
    current_role = None
    current_text = []

    for line in content.split('\n'):
        line = line.rstrip()

        # Skip header lines
        if line.startswith('AEYNIS CONVERSATION') or line.startswith('Timestamp:'):
            continue

        if line.startswith('USER: '):
            if current_role and current_text:
                turns.append((current_role, '\n'.join(current_text).strip()))
            current_role = 'Jim'
            current_text = [line[6:]]  # Strip "USER: "
        elif line.startswith('ASSISTANT: '):
            if current_role and current_text:
                turns.append((current_role, '\n'.join(current_text).strip()))
            current_role = 'Aeynis'
            current_text = [line[11:]]  # Strip "ASSISTANT: "
        elif current_role:
            current_text.append(line)

    # Don't forget the last turn
    if current_role and current_text:
        turns.append((current_role, '\n'.join(current_text).strip()))

    return date_str, turns


def chunk_turns(turns: list, date_str: str, chunk_size: int = CHUNK_SIZE) -> list:
    """Group turns into chunks that fit within chunk_size."""
    chunks = []
    current_chunk = []
    current_size = 0

    for role, text in turns:
        # Clean up artifacts
        text = text.replace('(Continue the conversation organically)', '').strip()
        if not text:
            continue

        turn_text = f"{role}: {text}"
        turn_size = len(turn_text)

        if current_size + turn_size > chunk_size and current_chunk:
            # Save current chunk
            chunk_num = len(chunks) + 1
            chunk_content = '\n'.join(current_chunk)
            chunks.append((chunk_num, chunk_content))
            current_chunk = [turn_text]
            current_size = turn_size
        else:
            current_chunk.append(turn_text)
            current_size += turn_size

    # Don't forget the last chunk
    if current_chunk:
        chunk_num = len(chunks) + 1
        chunk_content = '\n'.join(current_chunk)
        chunks.append((chunk_num, chunk_content))

    return chunks


def detect_tags(text: str) -> list:
    """Detect relevant tags from content."""
    text_lower = text.lower()
    tags = ['consolidated', 'transcript']

    tag_keywords = {
        'lyra': 'lyra',
        'oliver': 'oliver',
        'cesspanardo': 'cesspanardo',
        'cespenardo': 'cesspanardo',
        'weather mage': 'weather_mage',
        'glass mountain': 'glass_mountain',
        'storm': 'storm',
        'fairy': 'fairy',
        'fairies': 'fairy',
        'pat ': 'pat',
        'cade': 'cade',
        'bridge keeper': 'bridge_keeper',
        'mondaye': 'mondaye',
    }

    for keyword, tag in tag_keywords.items():
        if keyword in text_lower and tag not in tags:
            tags.append(tag)

    return tags


def store_memory(content: str, tags: list) -> bool:
    """Store a memory via the API."""
    try:
        response = requests.post(
            f"{MCP_MEMORY_URL}/api/memories",
            json={"content": content, "tags": tags},
            timeout=10,
        )
        return response.status_code in (200, 201)
    except Exception as e:
        print(f"  ERROR storing memory: {e}")
        return False


def ingest_file(filepath: str):
    """Ingest a single transcript file."""
    print(f"\n{'='*60}")
    print(f"Ingesting: {os.path.basename(filepath)}")
    print(f"{'='*60}")

    date_str, turns = parse_transcript(filepath)
    print(f"  Date: {date_str}")
    print(f"  Turns: {len(turns)}")

    if not turns:
        print("  No turns found, skipping")
        return 0

    chunks = chunk_turns(turns, date_str)
    print(f"  Chunks: {len(chunks)}")

    stored = 0
    for chunk_num, chunk_content in chunks:
        tags = detect_tags(chunk_content)
        # Prefix with date and chunk info for context
        memory_content = f"[Transcript {date_str} part {chunk_num}/{len(chunks)}] {chunk_content}"

        if store_memory(memory_content, tags):
            stored += 1
            print(f"  Stored chunk {chunk_num}/{len(chunks)} ({len(memory_content)} chars, tags: {tags})")
        else:
            print(f"  FAILED chunk {chunk_num}/{len(chunks)}")

    print(f"  Done: {stored}/{len(chunks)} chunks stored")
    return stored


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ingest_transcripts.py <transcript_file> [...]")
        print("Example: python3 ingest_transcripts.py ~/Downloads/aeynis_2026-03-14T*.txt")
        sys.exit(1)

    files = sys.argv[1:]
    total_stored = 0

    # Verify memory service is running
    try:
        r = requests.get(f"{MCP_MEMORY_URL}/api/memories", timeout=5)
        if r.status_code != 200:
            print("Memory service not responding properly")
            sys.exit(1)
    except requests.ConnectionError:
        print("Memory service not running. Start it first.")
        sys.exit(1)

    for filepath in sorted(files):
        if not os.path.exists(filepath):
            print(f"File not found: {filepath}")
            continue
        total_stored += ingest_file(filepath)

    print(f"\n{'='*60}")
    print(f"Total: {total_stored} memory chunks stored from {len(files)} files")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
