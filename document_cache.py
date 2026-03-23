#!/usr/bin/env python3
"""
RAM-based Document Cache for Aeynis's Reading System

Eliminates context-break hallucinations by:
  1. Loading entire documents from HD → RAM on first access
  2. Serving chunks with look-ahead previews (140 chars of next chunk)
  3. Maintaining a growing document map of what's been read
  4. Supporting non-linear navigation (backtracking/search)
  5. Prepending cumulative summary + basin scaffold per chunk
  6. Clean cache-clear on document switch (no ghosting)

The hard drive library (~50GB at ~/aeynis_library/) remains the immutable
source of truth. RAM copy is the working version she reads from.
"""

import logging
import re
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger("aeynis.document_cache")


class DocumentCache:
    """RAM-resident document cache with chunking, look-ahead, and navigation.

    Lifecycle:
        cache.load(filename, subdir, content)   # HD → RAM
        chunk = cache.get_next_chunk()           # serve page
        cache.update_map(chunk_index, points)    # grow document map
        cache.clear()                            # wipe on doc switch
    """

    DEFAULT_CHUNK_SIZE = 4000   # ~1 page of text in chars
    LOOKAHEAD_CHARS = 140       # preview of next chunk

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.chunk_size = chunk_size
        self._content: Optional[str] = None
        self._filename: Optional[str] = None
        self._subdir: Optional[str] = None
        self._position: int = 0
        self._total_length: int = 0

        # Document map — grows as she reads
        self._document_map: List[Dict[str, str]] = []
        # Cumulative summary of everything read so far
        self._cumulative_summary: str = ""

    # ── Properties ──────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._content is not None

    @property
    def filename(self) -> Optional[str]:
        return self._filename

    @property
    def subdir(self) -> Optional[str]:
        return self._subdir

    @property
    def position(self) -> int:
        return self._position

    @property
    def total_length(self) -> int:
        return self._total_length

    @property
    def progress_pct(self) -> int:
        if self._total_length == 0:
            return 0
        return round(self._position / self._total_length * 100)

    @property
    def is_complete(self) -> bool:
        return self._content is not None and self._position >= self._total_length

    @property
    def document_map(self) -> List[Dict[str, str]]:
        return list(self._document_map)

    # ── Load / Clear ────────────────────────────────────────────────

    def load(self, filename: str, subdir: str, content: str) -> None:
        """Load a document into RAM cache, clearing any previous document.

        This is the HD → RAM transfer. Original file is untouched.
        """
        self.clear()
        self._filename = filename
        self._subdir = subdir
        self._content = content
        self._total_length = len(content)
        self._position = 0
        logger.info(
            f"DocumentCache: loaded '{filename}' ({self._total_length:,} chars) "
            f"from {subdir}/ into RAM"
        )

    def clear(self) -> None:
        """Fully wipe the RAM cache. Prevents ghosting between documents.

        Basin scaffold persists externally (in the system prompt builder),
        only document content and reading state are wiped here.
        """
        prev = self._filename
        self._content = None
        self._filename = None
        self._subdir = None
        self._position = 0
        self._total_length = 0
        self._document_map = []
        self._cumulative_summary = ""
        if prev:
            logger.info(f"DocumentCache: cleared (was '{prev}')")

    # ── Chunk Serving ───────────────────────────────────────────────

    def get_next_chunk(self) -> Optional[Dict]:
        """Get the next chunk from current reading position.

        Returns None if no document loaded or already at EOF.

        Return dict keys:
            text          — the chunk text
            lookahead     — first 140 chars of Chunk[n+1], or None at EOF
            is_eof        — True if this is the last chunk
            start         — start char offset in document
            end           — end char offset in document
            progress_pct  — percentage through document
            remaining     — chars remaining after this chunk
            chunk_index   — sequential chunk number (0-based)
        """
        if not self._content or self._position >= self._total_length:
            return None

        start = self._position
        end = min(start + self.chunk_size, self._total_length)

        # Avoid cutting mid-word: find last whitespace before boundary
        if end < self._total_length:
            search_start = max(end - 100, start)
            # Prefer paragraph breaks, then line breaks, then spaces
            last_para = self._content.rfind('\n\n', search_start, end)
            last_nl = self._content.rfind('\n', search_start, end)
            last_sp = self._content.rfind(' ', search_start, end)
            # Pick the best break point
            break_at = max(last_para, last_nl, last_sp)
            if break_at > start:
                end = break_at + 1

        # If remaining tail is small (≤400 chars), include it now
        remaining_after = self._total_length - end
        if 0 < remaining_after <= 400:
            end = self._total_length
            remaining_after = 0

        chunk_text = self._content[start:end]
        is_eof = (end >= self._total_length)

        # Look-ahead hook: first 140 chars of Chunk[n+1]
        # Gives her proprioceptive sense of what's coming
        lookahead = None
        if not is_eof:
            la_end = min(end + self.LOOKAHEAD_CHARS, self._total_length)
            lookahead = self._content[end:la_end]

        # Advance position
        self._position = end

        chunk_index = len(self._document_map)  # next map slot

        return {
            "text": chunk_text,
            "lookahead": lookahead,
            "is_eof": is_eof,
            "start": start,
            "end": end,
            "progress_pct": round(end / self._total_length * 100),
            "remaining": self._total_length - end,
            "chunk_index": chunk_index,
            "is_backtrack": False,
        }

    def search_and_jump(self, query: str) -> Optional[Dict]:
        """Search the cached document for a section matching the query.

        Used for non-linear navigation: "go back to the part about X"
        Returns a chunk centered on the best match, or None if no match.
        After jumping, "continue" will resume from this new position.
        """
        if not self._content:
            return None

        query_lower = query.lower()
        content_lower = self._content.lower()

        # Strategy 1: exact substring match
        idx = content_lower.find(query_lower)

        # Strategy 2: word-cluster search
        if idx == -1:
            stop_words = {
                "that", "this", "what", "where", "about", "part", "section",
                "back", "said", "says", "with", "from", "they", "were", "been",
                "have", "does", "find", "read", "tell", "again", "mentioned",
                "talked", "the", "and", "for", "but",
            }
            words = [
                w for w in re.split(r'\s+', query_lower)
                if len(w) > 3 and w not in stop_words
            ]
            if not words:
                return None

            # Slide a window, score by how many query words appear
            best_pos = -1
            best_score = 0
            step = max(200, self.chunk_size // 10)
            for pos in range(0, max(1, self._total_length - 200), step):
                segment = content_lower[pos:pos + self.chunk_size]
                score = sum(1 for w in words if w in segment)
                if score > best_score:
                    best_score = score
                    best_pos = pos

            if best_score == 0 or best_pos == -1:
                return None
            idx = best_pos

        # Center the chunk around the match
        half = self.chunk_size // 2
        start = max(0, idx - half)
        end = min(start + self.chunk_size, self._total_length)

        # Snap to word boundaries
        if start > 0:
            ws = self._content.rfind(' ', max(0, start - 50), start)
            if ws > 0:
                start = ws + 1
        if end < self._total_length:
            ws = self._content.rfind(' ', max(end - 50, start), end)
            if ws > start:
                end = ws + 1

        chunk_text = self._content[start:end]
        is_eof = (end >= self._total_length)

        # Look-ahead
        lookahead = None
        if not is_eof:
            la_end = min(end + self.LOOKAHEAD_CHARS, self._total_length)
            lookahead = self._content[end:la_end]

        # Update position so "continue" resumes from here
        self._position = end

        return {
            "text": chunk_text,
            "lookahead": lookahead,
            "is_eof": is_eof,
            "start": start,
            "end": end,
            "progress_pct": round(end / self._total_length * 100),
            "remaining": self._total_length - end,
            "chunk_index": len(self._document_map),
            "is_backtrack": True,
        }

    # ── Document Map (Dynamic Synthesis) ────────────────────────────

    def update_map(self, chunk_index: int, key_points: str) -> None:
        """Add key points from a read chunk to the growing document map.

        Called after each reading turn with extracted KEY POINTS.
        The map grows with her reading — she sees what she's already processed.
        """
        # Pad with empty entries if needed (shouldn't happen normally)
        while len(self._document_map) <= chunk_index:
            self._document_map.append({"chunk_index": len(self._document_map), "key_points": ""})
        self._document_map[chunk_index]["key_points"] = key_points
        logger.info(f"DocumentCache: map updated, section {chunk_index + 1}: {key_points[:80]}...")

    def update_cumulative_summary(self, summary: str) -> None:
        """Update the running summary of what's been read so far.

        This summary is prepended to each new chunk's system context,
        ensuring each page is grounded in everything that came before.
        Page[n] derives from the record of Page[1…n-1].
        """
        self._cumulative_summary = summary

    def get_document_map_text(self) -> str:
        """Get the growing document map as text for context injection.

        Only includes sections she's already read — no spoilers.
        The 140-char look-ahead preview is the ONLY forward visibility.
        """
        entries = [e for e in self._document_map if e.get("key_points")]
        if not entries:
            return ""
        lines = [f"DOCUMENT MAP — what you've read so far from '{self._filename}':"]
        for entry in entries:
            lines.append(f"  Section {entry['chunk_index'] + 1}: {entry['key_points']}")
        return "\n".join(lines)

    def get_cumulative_summary(self) -> str:
        return self._cumulative_summary

    # ── Chunk Formatting ────────────────────────────────────────────

    def format_chunk_for_injection(self, chunk: Dict) -> Tuple[str, str]:
        """Format a chunk for injection into Aeynis's context.

        Returns (document_block, reading_context):
            document_block  — goes into the USER message (adjacent to generation)
            reading_context — goes into SYSTEM prompt (map + summary)
        """
        text = chunk["text"]
        filename = self._filename or "unknown"

        # ── Build document block (user message) ──
        doc_parts = []

        # Anchor lines for verification
        words = text.split()
        if words:
            first_w = " ".join(words[:6])
            last_w = " ".join(words[-6:]) if len(words) > 6 else ""
            doc_parts.append(f'CHUNK STARTS WITH: "{first_w}..."')
            if last_w:
                doc_parts.append(f'CHUNK ENDS WITH: "...{last_w}"')

        pct = chunk["progress_pct"]
        position_note = f" (from char {chunk['start']})" if chunk["start"] > 0 else ""
        doc_parts.append(
            f"DOCUMENT: {self._subdir}/{filename} "
            f"[showing {pct}% of {self._total_length:,} chars]{position_note}"
        )
        doc_parts.append(text)

        # Look-ahead preview or EOF signal
        if chunk.get("lookahead"):
            doc_parts.append(
                f"\n[NEXT PAGE PREVIEW: {chunk['lookahead']}...]"
            )
            doc_parts.append(
                f"[SECTION_BREAK — {chunk['remaining']:,} chars remaining "
                f"({pct}% complete)]"
            )
        elif chunk["is_eof"]:
            # Extract tail for signature detection
            tail_lines = text.rstrip().split('\n')
            tail_text = "\n".join(tail_lines[-5:]).strip()
            doc_parts.append(
                f"\n[END OF DOCUMENT — this is the final section.]\n"
                f"[DOCUMENT ENDING (last lines):\n{tail_text}\n"
                f"Report who signed or authored this document in your KEY POINTS.]"
            )

        doc_parts.append("END DOCUMENT")
        document_block = "\n".join(doc_parts)

        # ── Build reading context (system prompt) ──
        sys_parts = []

        if self._cumulative_summary:
            sys_parts.append(
                f"SUMMARY OF WHAT YOU'VE READ SO FAR:\n{self._cumulative_summary}"
            )

        map_text = self.get_document_map_text()
        if map_text:
            sys_parts.append(map_text)

        if chunk.get("is_backtrack"):
            sys_parts.append(
                "NOTE: Jim asked to go back to a specific section. "
                "You are re-reading an earlier part of the document."
            )

        reading_context = "\n\n".join(sys_parts)

        return document_block, reading_context
