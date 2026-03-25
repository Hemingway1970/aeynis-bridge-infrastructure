#!/usr/bin/env python3
"""
Aeynis Writing Tool - AbiWord-powered writing workspace for Aeynis

Provides natural writing capabilities integrated with the chat interface.
Aeynis can create, list, review, and update her own writings. The reflective
loop lets her see what she's written and load previous work into context.

Uses AbiWord as the word processor backend for document creation and
format conversion (Markdown → ODT, PDF, HTML, DOC).

Writings are stored in ~/AeynisLibrary/writings/.
Primary format: Markdown with YAML metadata header.
AbiWord handles export to other formats on demand.

No VRAM impact - runs on system RAM/CPU only.
"""

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from aeynis_library import AeynisLibrary, _safe_filename

logger = logging.getLogger(__name__)

WRITINGS_SUBDIR = "writings"

# AbiWord supported export formats
ABIWORD_FORMATS = {
    "odt": "odt",
    "pdf": "pdf",
    "html": "html",
    "doc": "doc",
    "rtf": "rtf",
    "txt": "txt",
}


def _check_abiword() -> bool:
    """Check if AbiWord is installed and available."""
    try:
        result = subprocess.run(
            ["abiword", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _abiword_convert(source_path: str, target_format: str, out_dir: str) -> Optional[str]:
    """Convert a document using AbiWord.

    Args:
        source_path:   Path to the source file
        target_format: Target format (odt, pdf, html, doc, rtf, txt)
        out_dir:       Directory for the output file

    Returns the path to the converted file, or None on failure.
    """
    if target_format not in ABIWORD_FORMATS:
        logger.warning(f"Unsupported AbiWord format: {target_format}")
        return None

    base = Path(source_path).stem
    out_path = os.path.join(out_dir, f"{base}.{target_format}")

    try:
        result = subprocess.run(
            ["abiword", "--to", target_format, "--to-name", out_path, source_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            logger.info(f"AbiWord converted {os.path.basename(source_path)} → {target_format}")
            return out_path
        else:
            logger.warning(f"AbiWord conversion failed: {result.stderr.strip()}")
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"AbiWord conversion error: {e}")
        return None


class AeynisWriting:
    """
    Manages Aeynis's personal writing workspace using AbiWord.

    Capabilities:
      - save_writing()     : Save a new piece of writing
      - list_writings()    : List all her writings with metadata
      - load_writing()     : Load a specific writing into context
      - update_writing()   : Append to or revise an existing piece
      - delete_writing()   : Remove a writing
      - search_writings()  : Find writings by keyword in title/content
      - export_writing()   : Convert a writing to another format via AbiWord
    """

    def __init__(self, library: AeynisLibrary):
        self.library = library
        self.writings_dir = os.path.join(library.root, WRITINGS_SUBDIR)
        os.makedirs(self.writings_dir, exist_ok=True)
        self.abiword_available = _check_abiword()
        if self.abiword_available:
            logger.info(f"AeynisWriting initialized at {self.writings_dir} (AbiWord available)")
        else:
            logger.warning(f"AeynisWriting initialized at {self.writings_dir} (AbiWord NOT found — install with: sudo apt install abiword)")

    def save_writing(self, title: str, content: str,
                     tags: Optional[List[str]] = None,
                     export_format: str = "") -> Dict:
        """Save a new piece of writing.

        Args:
            title:         Title for the piece
            content:       The writing content (Aeynis's generated text)
            tags:          Optional tags (e.g., ["reflection", "poetry", "synthesis"])
            export_format: Optional format to export via AbiWord (odt, pdf, html, doc)

        Returns dict with success, filename, path, and optional export info.
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
            result["abiword_available"] = self.abiword_available
            logger.info(f"Saved writing '{title}' as {filename}")

            # Export to additional format via AbiWord if requested
            if export_format and self.abiword_available:
                source_path = os.path.join(self.writings_dir, filename)
                export_result = _abiword_convert(source_path, export_format, self.writings_dir)
                if export_result:
                    result["exported_to"] = export_format
                    result["export_path"] = os.path.relpath(export_result, self.library.root)
                else:
                    result["export_error"] = f"AbiWord could not convert to {export_format}"

        return result

    def export_writing(self, identifier: str, target_format: str) -> Dict:
        """Export a writing to another format using AbiWord.

        Args:
            identifier:    Filename or title to match
            target_format: Target format (odt, pdf, html, doc, rtf, txt)

        Returns dict with success, export_path.
        """
        if not self.abiword_available:
            return {
                "error": "AbiWord is not installed. Install with: sudo apt install abiword",
                "success": False,
            }

        if target_format not in ABIWORD_FORMATS:
            return {
                "error": f"Unsupported format: {target_format}. Supported: {', '.join(ABIWORD_FORMATS.keys())}",
                "success": False,
            }

        # Find the source file
        existing = self.load_writing(identifier)
        if not existing.get("success"):
            return existing

        source_filename = existing["filename"]
        source_path = os.path.join(self.writings_dir, source_filename)

        if not os.path.isfile(source_path):
            return {"error": f"Source file not found: {source_filename}", "success": False}

        export_path = _abiword_convert(source_path, target_format, self.writings_dir)
        if export_path:
            return {
                "success": True,
                "title": existing.get("title", identifier),
                "source": source_filename,
                "exported_to": target_format,
                "export_path": os.path.relpath(export_path, self.library.root),
                "export_filename": os.path.basename(export_path),
            }
        return {
            "error": f"AbiWord failed to convert '{source_filename}' to {target_format}",
            "success": False,
        }

    def list_writings(self) -> List[Dict]:
        """List all writings with parsed metadata."""
        files = self.library.list_files(WRITINGS_SUBDIR)
        writings = []

        for f in files:
            if f.get("type") == "directory":
                continue
            # Include all document types, not just .md
            name = f["name"]
            ext = Path(name).suffix.lower()
            if ext not in (".md", ".odt", ".pdf", ".html", ".doc", ".rtf", ".txt"):
                continue

            # Parse metadata from header (only works for .md files)
            meta = {}
            if ext == ".md":
                meta = self._parse_header(name)

            writings.append({
                "filename": name,
                "title": meta.get("title", name.rsplit(".", 1)[0].replace("_", " ")),
                "date": meta.get("date", f.get("modified", "")),
                "tags": meta.get("tags", []),
                "size": f.get("size_human", ""),
                "format": ext.lstrip("."),
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

            # Check content match (only for text-readable formats)
            if w.get("format") in ("md", "txt", "html", "rtf"):
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
            fmt_str = f" ({w['format']})" if w.get("format", "md") != "md" else ""
            lines.append(f"  - {w['title']} ({w['date']}{tag_str}){fmt_str} [{w['size']}]")

        listing = "\n".join(lines[:15])  # Cap to save context budget
        abiword_note = " (AbiWord available for export)" if self.abiword_available else ""
        return (
            f"\nYOUR WRITINGS ({len(writings)} pieces{abiword_note}):\n"
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
