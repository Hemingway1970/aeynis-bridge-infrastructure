#!/usr/bin/env python3
"""
Aeynis Writing Tool - Persistent writing workspace for Aeynis

Provides natural writing capabilities integrated with the chat interface.
Aeynis can create, list, review, and update her own writings. The reflective
loop lets her see what she's written and load previous work into context.

Writings are stored as Markdown files in ~/AeynisLibrary/writings/.
Each file has a YAML-style header with metadata (title, date, tags).

No VRAM impact - runs on system RAM/CPU only.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from aeynis_library import AeynisLibrary, _safe_filename

logger = logging.getLogger(__name__)

WRITINGS_SUBDIR = "writings"


class AeynisWriting:
    """
    Manages Aeynis's personal writing workspace.

    Capabilities:
      - save_writing()     : Save a new piece of writing
      - list_writings()    : List all her writings with metadata
      - load_writing()     : Load a specific writing into context
      - update_writing()   : Append to or revise an existing piece
      - delete_writing()   : Remove a writing
      - search_writings()  : Find writings by keyword in title/content
    """

    def __init__(self, library: AeynisLibrary):
        self.library = library
        self.writings_dir = os.path.join(library.root, WRITINGS_SUBDIR)
        os.makedirs(self.writings_dir, exist_ok=True)
        logger.info(f"AeynisWriting initialized at {self.writings_dir}")

    def save_writing(self, title: str, content: str,
                     tags: Optional[List[str]] = None) -> Dict:
        """Save a new piece of writing.

        Args:
            title:   Title for the piece
            content: The writing content (Aeynis's generated text)
            tags:    Optional tags (e.g., ["reflection", "poetry", "synthesis"])

        Returns dict with success, filename, path.
        """
        timestamp = datetime.now()
        safe_title = _safe_filename(title)
        if not safe_title or safe_title == "untitled":
            safe_title = f"writing_{timestamp.strftime('%Y%m%d_%H%M%S')}"

        filename = f"{safe_title}.md"

        # Build the document with metadata header
        tag_str = ", ".join(tags) if tags else ""
        header = (
            f"---\n"
            f"title: {title}\n"
            f"author: Aeynis\n"
            f"date: {timestamp.strftime('%Y-%m-%d %H:%M')}\n"
            f"tags: {tag_str}\n"
            f"---\n\n"
        )

        full_content = header + content

        result = self.library.write_file(
            filename=filename,
            content=full_content,
            subdir=WRITINGS_SUBDIR,
            fmt="md",
        )

        if result.get("success"):
            result["title"] = title
            result["tags"] = tags or []
            logger.info(f"Saved writing '{title}' as {filename}")

        return result

    def list_writings(self) -> List[Dict]:
        """List all writings with parsed metadata."""
        files = self.library.list_files(WRITINGS_SUBDIR)
        writings = []

        for f in files:
            if f.get("type") == "directory":
                continue
            if not f["name"].endswith(".md"):
                continue

            # Parse metadata from header
            meta = self._parse_header(f["name"])
            writings.append({
                "filename": f["name"],
                "title": meta.get("title", f["name"].replace(".md", "").replace("_", " ")),
                "date": meta.get("date", f.get("modified", "")),
                "tags": meta.get("tags", []),
                "size": f.get("size_human", ""),
            })

        # Sort by date, newest first
        writings.sort(key=lambda w: w["date"], reverse=True)
        return writings

    def load_writing(self, identifier: str) -> Dict:
        """Load a writing by filename or title match.

        Args:
            identifier: Filename or partial title to match

        Returns dict with success, content, metadata.
        """
        # Try exact filename first
        result = self.library.read_file(identifier, WRITINGS_SUBDIR)
        if result.get("success"):
            meta = self._parse_header_from_content(result["content"])
            result["title"] = meta.get("title", identifier)
            result["tags"] = meta.get("tags", [])
            result["body"] = self._strip_header(result["content"])
            return result

        # Try matching by title/partial name
        writings = self.list_writings()
        id_lower = identifier.lower()

        for w in writings:
            if (id_lower in w["title"].lower()
                    or id_lower in w["filename"].lower()):
                result = self.library.read_file(w["filename"], WRITINGS_SUBDIR)
                if result.get("success"):
                    meta = self._parse_header_from_content(result["content"])
                    result["title"] = meta.get("title", w["title"])
                    result["tags"] = meta.get("tags", [])
                    result["body"] = self._strip_header(result["content"])
                    return result

        return {"error": f"No writing found matching '{identifier}'", "success": False}

    def update_writing(self, identifier: str, additional_content: str) -> Dict:
        """Append new content to an existing writing.

        Args:
            identifier:         Filename or title to match
            additional_content: Text to append

        Returns dict with success status.
        """
        existing = self.load_writing(identifier)
        if not existing.get("success"):
            return existing

        # Append with a continuation marker
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        separator = f"\n\n---\n*Continued {timestamp}*\n\n"
        new_content = existing["content"] + separator + additional_content

        filename = existing["filename"]
        return self.library.write_file(
            filename=filename,
            content=new_content,
            subdir=WRITINGS_SUBDIR,
            fmt="md",
        )

    def delete_writing(self, identifier: str) -> Dict:
        """Delete a writing by filename or title match."""
        # Try exact filename
        result = self.library.delete_file(identifier, WRITINGS_SUBDIR)
        if result.get("success"):
            return result

        # Try matching
        writings = self.list_writings()
        id_lower = identifier.lower()
        for w in writings:
            if id_lower in w["title"].lower() or id_lower in w["filename"].lower():
                return self.library.delete_file(w["filename"], WRITINGS_SUBDIR)

        return {"error": f"No writing found matching '{identifier}'", "success": False}

    def search_writings(self, query: str) -> List[Dict]:
        """Search writings by keyword in title and content."""
        results = []
        query_lower = query.lower()
        writings = self.list_writings()

        for w in writings:
            # Check title match
            if query_lower in w["title"].lower():
                results.append({**w, "match": "title"})
                continue

            # Check content match
            full = self.library.read_file(w["filename"], WRITINGS_SUBDIR)
            if full.get("success") and query_lower in full["content"].lower():
                # Extract a snippet around the match
                content_lower = full["content"].lower()
                idx = content_lower.find(query_lower)
                start = max(0, idx - 80)
                end = min(len(full["content"]), idx + len(query) + 80)
                snippet = full["content"][start:end].strip()
                results.append({**w, "match": "content", "snippet": f"...{snippet}..."})

        return results

    def format_listing_for_context(self) -> str:
        """Format writings list for injection into system prompt."""
        writings = self.list_writings()
        if not writings:
            return ""

        lines = []
        for w in writings:
            tag_str = f" [{', '.join(w['tags'])}]" if w.get("tags") else ""
            lines.append(f"  - {w['title']} ({w['date']}{tag_str}) [{w['size']}]")

        listing = "\n".join(lines[:15])  # Cap to save context budget
        return (
            f"\nYOUR WRITINGS ({len(writings)} pieces):\n"
            f"{listing}\n"
            f"{'  (... and more)' if len(writings) > 15 else ''}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_header(self, filename: str) -> Dict:
        """Parse YAML-style header from a writing file."""
        result = self.library.read_file(filename, WRITINGS_SUBDIR)
        if not result.get("success"):
            return {}
        return self._parse_header_from_content(result["content"])

    @staticmethod
    def _parse_header_from_content(content: str) -> Dict:
        """Parse YAML-style header from content string."""
        meta = {}
        header_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not header_match:
            return meta

        for line in header_match.group(1).split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                key = key.strip().lower()
                value = value.strip()
                if key == "tags" and value:
                    meta["tags"] = [t.strip() for t in value.split(",") if t.strip()]
                else:
                    meta[key] = value
        return meta

    @staticmethod
    def _strip_header(content: str) -> str:
        """Remove the YAML header, returning just the body."""
        stripped = re.sub(r'^---\s*\n.*?\n---\s*\n*', '', content, count=1, flags=re.DOTALL)
        return stripped.strip()
