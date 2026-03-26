#!/usr/bin/env python3
"""
Aeynis Chat Backend
Wires together mcp-memory-service, Augustus basins, and KoboldCpp for interactive chat with Aeynis
"""

import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime
from typing import Dict, List, Any, Optional

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

from aeynis_library_api import library_bp, init_library, get_library
from aeynis_writing_api import writings_bp, init_writing_tool, get_writing_tool
from aeynis_calendar_api import calendar_bp, init_calendar, get_calendar
from document_cache import DocumentCache
from image_viewer_api import images_bp, init_image_viewer, get_image_viewer
from image_viewer import IMAGES_ROOT

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow Lux's frontend to connect

# Register the Library blueprint and initialize with default path
app.register_blueprint(library_bp)
_library = init_library()  # Creates ~/AeynisLibrary with 50GB quota

# Register the Image Viewer blueprint
app.register_blueprint(images_bp)

# Register the Writing Tool blueprint
app.register_blueprint(writings_bp)
init_writing_tool(_library)

# Register the Calendar blueprint
app.register_blueprint(calendar_bp)
init_calendar(_library.root)

# Backend URLs
KOBOLD_URL = "http://localhost:5001"
AUGUSTUS_URL = "http://localhost:8080"
MCP_MEMORY_URL = "http://localhost:8000"

# Configuration
AGENT_ID = "aeynis"
MAX_CONTEXT_MEMORIES = 15
MAX_CONVERSATION_TURNS = 40       # Max exchanges before trimming history
MAX_PROMPT_CHARS = 8000           # Conservative limit for Mistral-Nemo context window
CONTEXT_WARNING_THRESHOLD = 6000  # Warn when prompt approaches limit
# Document injection budgets are now handled by DocumentCache.chunk_size (~4000 chars)
# The cache loads the entire file into RAM and serves consistent chunks with look-ahead.

class AeynisChat:
    """Main chat orchestrator integrating all three backends"""

    def __init__(self):
        self.conversation_history = []

        # RAM-based document cache — replaces the old offset-tracking approach
        self._doc_cache = DocumentCache()

        # Turn-level flags (reset each turn in generate_response)
        self._is_continue_read = False    # True when processing a "continue reading" request
        self._reading_doc = False         # True when a document chunk was injected this turn
        self._reading_doc_name = ""       # Filename being read (for memory tagging)
        self._reading_context = ""        # Map + summary context for system prompt (set by _detect_and_inject)

        # Reading idle tracking — auto-clear cache after conversation moves on
        self._turns_since_last_read = 0   # Incremented each non-reading turn
        self._reading_idle_limit = 3      # Clear cache after this many non-reading turns

        # Post-read follow-up support
        self._post_read_context = ""      # Summary of recently-read doc for follow-up questions
        self._post_read_turns = 0         # Turns remaining to show post-read context

        # Track the last chunk for map updates after response
        self._last_chunk_info: Optional[Dict] = None

        # Image viewer integration
        self._viewing_image = False       # True when an image perception was injected this turn
        self._viewing_image_name = ""     # Filename being viewed
        self._images_root = IMAGES_ROOT   # For building serve URLs in responses

        # Writing tool integration
        self._writing_mode = False        # True when Aeynis is composing a piece this turn
        self._writing_title = ""          # Title for the piece being written
        self._writing_tags = []           # Tags for the piece being written

        # Calendar integration
        self._calendar_action = ""        # "add", "query", or "" for this turn
        self._calendar_data = {}          # Extracted event data for this turn

        logger.info("Aeynis Chat Backend initialized")
    
    async def retrieve_relevant_memories(self, query: str, n_results: int = MAX_CONTEXT_MEMORIES) -> List[Dict]:
        """Retrieve relevant memories from mcp-memory-service using semantic search.

        Prioritizes consolidated summaries over individual fragments, since
        summaries contain richer context that the model can use more effectively.
        """
        try:
            logger.info(f"Searching {n_results} relevant memories for query: {query[:50]}...")

            response = requests.post(
                f"{MCP_MEMORY_URL}/api/search",
                json={"query": query, "n_results": n_results * 2},  # Fetch extra to allow sorting
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])

                # Separate consolidated summaries from raw fragments
                consolidated = []
                fragments = []
                for r in results:
                    mem = r.get('memory', {})
                    score = r.get('similarity_score', 0)
                    tags = mem.get('tags', [])
                    if 'consolidated' in tags:
                        consolidated.append((mem, score))
                    else:
                        fragments.append((mem, score))

                # Prioritize consolidated memories, then fill with fragments
                ordered = consolidated + fragments
                memories = [m for m, s in ordered[:n_results]]
                scores = [s for m, s in ordered[:n_results]]
                n_cons = min(len(consolidated), n_results)
                logger.info(f"Found {len(memories)} memories ({n_cons} consolidated, "
                            f"{len(memories) - n_cons} fragments, "
                            f"scores: {[f'{s:.2f}' for s in scores]})")
                return memories

            # Fallback to flat list if search endpoint fails
            logger.warning(f"Search returned {response.status_code}, falling back to list")
            response = requests.get(f"{MCP_MEMORY_URL}/api/memories", timeout=5)
            if response.status_code == 200:
                data = response.json()
                return data.get('memories', [])[:n_results]
            return []

        except Exception as e:
            logger.error(f"Error retrieving memories: {e}")
            return []
    
    async def get_basin_context(self) -> Dict:
        """Get current basin state from Augustus"""
        try:
            response = requests.get(f"{AUGUSTUS_URL}/api/agents/{AGENT_ID}")
            if response.status_code == 200:
                agent_data = response.json()
                basins = agent_data.get('basins', [])
                
                # Format basin context for prompt
                basin_context = "\n".join([
                    f"- {b['name']}: α={b['alpha']:.2f} (λ={b['lambda']}, η={b['eta']})"
                    for b in basins
                ])
                
                return {
                    'basins': basins,
                    'context': basin_context,
                    'emphasis_directive': agent_data.get('emphasis_directive', '')
                }
            else:
                logger.warning(f"Failed to fetch basin state: {response.status_code}")
                return {'basins': [], 'context': '', 'emphasis_directive': ''}
                
        except Exception as e:
            logger.error(f"Error getting basin context: {e}")
            return {'basins': [], 'context': '', 'emphasis_directive': ''}
    
    def _get_library_context(self) -> str:
        """Build a summary of what's in the library for the system prompt."""
        try:
            lib = get_library()
            all_files = []
            for subdir in ["imports", "originals", "reviews", "writings"]:
                files = lib.list_files(subdir)
                for f in files:
                    if f.get("type") != "directory":
                        all_files.append(f"  - {subdir}/{f['name']} ({f['size_human']})")
            if not all_files:
                return ""
            listing = "\n".join(all_files[:20])  # Cap at 20 files to save context
            return (
                f"\nYOUR LIBRARY (files Jim has given you or you have written):\n"
                f"{listing}\n"
                f"{'  (... and more)' if len(all_files) > 20 else ''}"
            )
        except Exception as e:
            logger.error(f"Error building library context: {e}")
            return ""

    def _format_post_read_context(self) -> str:
        """Return recently-read document summary for the system prompt, if any."""
        if self._post_read_context and self._post_read_turns > 0:
            self._post_read_turns -= 1
            return (
                f"\nYOUR READING NOTES (reference for follow-up discussion - do NOT re-read or produce KEY POINTS from these; just use them to inform your conversation):\n"
                f"{self._post_read_context}\n"
            )
        return ""

    def _retrieve_reading_notes(self, filename: str) -> str:
        """Retrieve prior reading notes for a document from memory.

        When Aeynis reads a document chunk-by-chunk, each chunk's key points
        are stored as a memory tagged 'reading_note:<filename>'. This method
        retrieves those notes so she has context from previous chunks.
        """
        try:
            tag = f"reading_note:{filename}"
            response = requests.post(
                f"{MCP_MEMORY_URL}/api/search",
                json={"query": f"reading notes for {filename}", "n_results": 20},
                timeout=5,
            )
            if response.status_code != 200:
                return ""

            results = response.json().get("results", [])
            notes = []
            for r in results:
                mem = r.get("memory", {})
                tags = mem.get("tags", [])
                if tag in tags:
                    notes.append(mem["content"])

            if not notes:
                return ""

            # Combine notes, cap total length to protect context budget
            combined = "\n".join(notes)
            if len(combined) > 1500:
                combined = combined[:1500] + "\n[... earlier notes truncated]"

            return combined

        except Exception as e:
            logger.error(f"Error retrieving reading notes: {e}")
            return ""

    def _store_reading_note(self, filename: str, note_content: str, is_final: bool = False):
        """Store a reading note for a document chunk in memory."""
        try:
            tag = f"reading_note:{filename}"
            tags = ["reading_note", tag, "aeynis"]
            if is_final:
                tags.append("reading_complete")

            requests.post(
                f"{MCP_MEMORY_URL}/api/memories",
                json={
                    "content": note_content,
                    "tags": tags,
                },
                timeout=5,
            )
            logger.info(f"Stored reading note for '{filename}' ({len(note_content)} chars, final={is_final})")
        except Exception as e:
            logger.error(f"Failed to store reading note: {e}")

    def _detect_backtrack_request(self, msg_lower: str) -> Optional[str]:
        """Detect if the user is asking to go back to a specific part of the document.

        Returns the search query if backtracking detected, None otherwise.
        Patterns: "go back to the part about X", "what did it say about Y",
                  "re-read the section on Z", "find where it mentioned [topic]"
        """
        backtrack_patterns = [
            r"(?:go\s+back|return)\s+to\s+(?:the\s+)?(?:part|section|bit|place)\s+(?:about|where|on|with)\s+(.+)",
            r"(?:re-?read|reread)\s+(?:the\s+)?(?:part|section|bit)?\s*(?:about|where|on|with)?\s*(.+)",
            r"what\s+did\s+it\s+say\s+(?:about|regarding|on)\s+(.+)",
            r"find\s+(?:the\s+)?(?:part|section|place)?\s*(?:where|about|that|when)\s+(?:it\s+)?(?:mentioned?|talked?|said?|discuss)\s*(.+)",
            r"(?:can\s+you\s+)?(?:go\s+back\s+to|find)\s+(?:where\s+)?(?:it\s+)?(?:mentions?|talks?\s+about|says?|discusses?)\s+(.+)",
        ]
        for pattern in backtrack_patterns:
            m = re.search(pattern, msg_lower)
            if m:
                query = m.group(1).strip().rstrip("?.,!")
                if len(query) > 2:
                    return query
        return None

    def _detect_and_inject_image(self, user_message: str) -> str:
        """Detect image viewing commands and inject perception into context.

        Handles:
          - "show me [folder]", "open [folder] images", "what's in the family folder"
          - "next image", "previous image", "go back"
          - "show me [filename]"
          - Image navigation while a folder is open

        Returns formatted image perception block for user message, or empty string.
        """
        try:
            viewer = get_image_viewer()
            msg_lower = user_message.lower().strip()

            # ── Navigation commands (when a folder is already open) ────
            if viewer.is_open:
                # Next image
                if re.search(r'\b(next|forward|advance)\b', msg_lower) and \
                   re.search(r'\b(image|photo|picture|pic|one)\b', msg_lower):
                    if viewer.next_image():
                        self._viewing_image = True
                        self._viewing_image_name = viewer.current_filename or ""
                        perception = viewer.view_current()
                        if perception:
                            return f"\n{viewer.format_perception_for_chat(perception)}\n"
                        return f"\n[Viewing image: {self._viewing_image_name}. VLM perception unavailable.]\n"
                    return "\n[Already at the last image in this folder.]\n"

                # Previous image
                if re.search(r'\b(prev|previous|back|prior|last)\b', msg_lower) and \
                   re.search(r'\b(image|photo|picture|pic|one)\b', msg_lower):
                    if viewer.prev_image():
                        self._viewing_image = True
                        self._viewing_image_name = viewer.current_filename or ""
                        perception = viewer.view_current()
                        if perception:
                            return f"\n{viewer.format_perception_for_chat(perception)}\n"
                        return f"\n[Viewing image: {self._viewing_image_name}. VLM perception unavailable.]\n"
                    return "\n[Already at the first image in this folder.]\n"

                # Short affirmatives while viewing → next image (like "continue" in reading)
                short_next = ["next", "keep going", "more", "go on", "continue",
                              "another", "what else", "show me more"]
                if any(kw in msg_lower for kw in short_next) and len(msg_lower.split()) <= 6:
                    if viewer.next_image():
                        self._viewing_image = True
                        self._viewing_image_name = viewer.current_filename or ""
                        perception = viewer.view_current()
                        if perception:
                            return f"\n{viewer.format_perception_for_chat(perception)}\n"
                        return f"\n[Viewing image: {self._viewing_image_name}. VLM perception unavailable.]\n"
                    return "\n[That's the last image in this folder.]\n"

                # Close/stop viewing
                if re.search(r'\b(close|stop|done|finish|exit)\b.*\b(view|image|photo|picture|folder)\b', msg_lower) or \
                   re.search(r'\b(view|image|photo|picture|folder)\b.*\b(close|stop|done|finish|exit)\b', msg_lower):
                    viewer.close_session()
                    return "\n[Image viewing session closed.]\n"

                # Jump to specific image by name
                show_match = re.search(r'show\s+(?:me\s+)?["\']?([^"\']+?)["\']?\s*$', msg_lower)
                if show_match:
                    target = show_match.group(1).strip()
                    if viewer.jump_to_filename(target):
                        self._viewing_image = True
                        self._viewing_image_name = viewer.current_filename or ""
                        perception = viewer.view_current()
                        if perception:
                            return f"\n{viewer.format_perception_for_chat(perception)}\n"
                        return f"\n[Viewing image: {self._viewing_image_name}. VLM perception unavailable.]\n"

            # ── "Pick one" / "look at an image" / random selection ─────
            random_patterns = [
                r'\b(?:pick|choose|select|grab)\s+(?:a|an|one|any|random)',
                r'\b(?:look\s+at|view|open|show)\s+(?:a|an|one|any|some)\s+(?:image|photo|picture|pic)\b',
                r'\bjust\s+(?:pick|choose|open|show)\s+(?:one|any|something)\b',
                r'\bshow\s+(?:me\s+)?(?:something|anything|a\s+(?:photo|image|picture))\b',
                r'\b(?:random|surprise)\s+(?:image|photo|picture|pic)\b',
                r'\blet(?:\'?s)?\s+(?:look\s+at|see|view)\s+(?:a|an|one|some)\s+(?:image|photo|picture)\b',
            ]
            if any(re.search(p, msg_lower) for p in random_patterns):
                folders = viewer.list_folders()
                # Filter to folders that actually have images
                nonempty = [f for f in folders if f.get("image_count", 0) > 0]
                if nonempty:
                    chosen = random.choice(nonempty)
                    result = viewer.open_folder(chosen["path"])
                    if result.get("success") and result["image_count"] > 0:
                        # Jump to a random image within the folder
                        rand_idx = random.randint(0, result["image_count"] - 1)
                        if rand_idx > 0:
                            viewer.jump_to(rand_idx)
                        # Always flag that we're viewing an image so the
                        # frontend shows the thumbnail even if VLM fails
                        self._viewing_image = True
                        self._viewing_image_name = viewer.current_filename or ""
                        perception = viewer.view_current()
                        header = f"[Opened folder '{chosen['name']}' — {result['image_count']} images, showing #{rand_idx + 1}]\n"
                        if perception:
                            return f"\n{header}{viewer.format_perception_for_chat(perception)}\n"
                        else:
                            logger.warning(f"VLM perception failed for '{self._viewing_image_name}', showing image without perception")
                            return f"\n{header}[Viewing image: {self._viewing_image_name}. VLM perception is unavailable — describe what you see from the filename and context.]\n"
                else:
                    return "\n[No image folders found. Place images in ~/AeynisLibrary/images/]\n"

            # ── "Can you see your library/images?" — list what's available ─
            library_check_patterns = [
                r'\b(?:see|find|access|locate|have)\s+(?:your|the|my)?\s*(?:image|photo|picture)?\s*(?:library|collection|folder)',
                r'\bdo\s+you\s+(?:see|have|find)\s+(?:your|the)?\s*(?:image|photo)?\s*(?:library|images|photos)',
                r'\byour\s+(?:image\s+)?library\b',
            ]
            if any(re.search(p, msg_lower) for p in library_check_patterns):
                folders = viewer.list_folders()
                if folders:
                    total_images = sum(f.get("image_count", 0) for f in folders)
                    listing = ", ".join(f"{f['name']} ({f['image_count']})" for f in folders)
                    return f"\n[IMAGE LIBRARY: {len(folders)} folders, {total_images} total images. Folders: {listing}. Say 'pick one' or name a folder to start viewing.]\n"
                return "\n[No image folders found in ~/AeynisLibrary/images/]\n"

            # ── Open folder commands ──────────────────────────────────
            folder_patterns = [
                r"(?:show|open|look\s+at|view|browse)\s+(?:me\s+)?(?:the\s+)?(?:images?\s+(?:in|from)\s+)?[\"']?(\w[\w\s-]*?)[\"']?\s*(?:folder|images?|photos?|pictures?|pics?)?$",
                r"what(?:'s| is)\s+in\s+(?:the\s+)?[\"']?(\w[\w\s-]*?)[\"']?\s*(?:folder|images?|photos?)?$",
                r"(?:let(?:'s| us)\s+)?(?:look\s+at|see|view)\s+(?:the\s+)?[\"']?(\w[\w\s-]*?)[\"']?\s*(?:folder|images?|photos?|pictures?)?$",
            ]

            for pattern in folder_patterns:
                m = re.search(pattern, msg_lower)
                if m:
                    folder_name = m.group(1).strip()
                    # Try to match against available folders
                    folders = viewer.list_folders()
                    matched = None
                    for f in folders:
                        if f["name"].lower() == folder_name.lower():
                            matched = f
                            break
                        if folder_name.lower() in f["name"].lower():
                            matched = f
                            break

                    if matched:
                        result = viewer.open_folder(matched["path"])
                        if result.get("success"):
                            # Auto-view first image — always flag as viewing so
                            # frontend shows thumbnail even if VLM fails
                            self._viewing_image = True
                            self._viewing_image_name = viewer.current_filename or ""
                            perception = viewer.view_current()
                            header = f"[Opened folder '{matched['name']}' — {result['image_count']} images]\n"
                            if perception:
                                return f"\n{header}{viewer.format_perception_for_chat(perception)}\n"
                            else:
                                return f"\n{header}[Viewing image: {self._viewing_image_name}. VLM perception unavailable.]\n"
                    else:
                        # List available folders
                        if folders:
                            listing = ", ".join(f["name"] + f" ({f['image_count']})" for f in folders)
                            return f"\n[No folder matching '{folder_name}'. Available: {listing}]\n"
                        return f"\n[No image folders found in {viewer.list_folders.__self__.__class__.__name__}. Place images in ~/AeynisLibrary/images/]\n"

            return ""

        except Exception as e:
            logger.error(f"Error in image command detection: {e}")
            return ""

    def _detect_and_inject_file_content(self, user_message: str) -> str:
        """If the user message references a file in the library, serve it from RAM cache.

        Flow:
          1. If actively reading AND user says "continue" / short affirmative → next chunk from cache
          2. If actively reading AND user asks to go back → search & jump in cache
          3. Otherwise, match filename → load full file HD→RAM (clearing any previous) → first chunk
        Returns formatted document block for user message, or empty string.
        """
        try:
            lib = get_library()
            msg_lower = user_message.lower()

            # ── Auto-expire reading cache after conversation moves on ──
            if self._doc_cache.is_loaded and self._turns_since_last_read >= self._reading_idle_limit:
                logger.info(f"Reading cache expired after {self._turns_since_last_read} idle turns — clearing '{self._doc_cache.filename}'")
                self._doc_cache.clear()

            # ── Continue reading (next chunk from RAM cache) ─────────────
            # Strong reading keywords — require explicit reading intent
            strong_read_kw = ["continue reading", "keep reading", "read on", "read more",
                              "next page", "next section", "next part", "the rest",
                              "what happens next", "carry on reading", "go on reading"]
            # Weaker keywords — only valid if we were JUST reading (0 idle turns)
            weak_read_kw = ["continue", "keep going", "go on", "go ahead",
                            "carry on", "more", "and then"]
            has_strong = any(kw in msg_lower for kw in strong_read_kw)
            has_weak = (self._turns_since_last_read == 0
                        and any(kw in msg_lower for kw in weak_read_kw))
            is_continue = (
                self._doc_cache.is_loaded
                and not self._doc_cache.is_complete
                and len(msg_lower.split()) <= 12
                and (has_strong or has_weak)
            )

            if is_continue:
                self._is_continue_read = True
                self._turns_since_last_read = 0  # Reset idle counter
                chunk = self._doc_cache.get_next_chunk()
                if not chunk:
                    return ""
                self._reading_doc = True
                self._reading_doc_name = self._doc_cache.filename or ""
                self._last_chunk_info = chunk
                document_block, reading_context = self._doc_cache.format_chunk_for_injection(chunk)
                self._reading_context = reading_context
                logger.info(
                    f"Served continue chunk for '{self._doc_cache.filename}' "
                    f"({chunk['progress_pct']}% complete, {chunk['remaining']:,} remaining)"
                )
                return f"\n{document_block}\n"

            # ── Backtrack request (search & jump within cached doc) ──────
            if self._doc_cache.is_loaded:
                backtrack_query = self._detect_backtrack_request(msg_lower)
                if backtrack_query:
                    chunk = self._doc_cache.search_and_jump(backtrack_query)
                    if chunk:
                        self._is_continue_read = True  # reset history for clean read
                        self._reading_doc = True
                        self._reading_doc_name = self._doc_cache.filename or ""
                        self._last_chunk_info = chunk
                        document_block, reading_context = self._doc_cache.format_chunk_for_injection(chunk)
                        self._reading_context = reading_context
                        logger.info(
                            f"Backtrack jump for '{self._doc_cache.filename}' "
                            f"to match '{backtrack_query}' at char {chunk['start']}"
                        )
                        return f"\n{document_block}\n"
                    else:
                        logger.info(f"Backtrack search found no match for '{backtrack_query}'")

            # ── New file reference (match filename → load HD→RAM) ────────
            known_files = {}  # lowercase filename -> (subdir, original_name)
            for subdir in ["imports", "originals", "reviews"]:
                for f in lib.list_files(subdir):
                    if f.get("type") != "directory":
                        known_files[f["name"].lower()] = (subdir, f["name"])

            if not known_files:
                return ""

            # Check if message contains reading-intent words (needed for fuzzy matching)
            reading_intent_words = {"read", "open", "look", "show", "file", "book",
                                    "paper", "pdf", "document", "letter", "article"}
            has_reading_intent = bool(msg_words & reading_intent_words)

            matched_file = None
            matched_subdir = None

            msg_normalized = msg_lower.replace("_", " ").replace("-", " ")
            noise_words = {"the", "a", "an", "and", "or", "but", "is", "are", "was",
                           "were", "be", "been", "to", "of", "in", "for", "on", "at",
                           "by", "it", "my", "me", "do", "can", "you", "she", "her",
                           "that", "this", "what", "from", "with", "about", "read",
                           "look", "show", "open", "tell", "file", "book", "paper",
                           "pdf", "document", "please", "could", "would", "have",
                           "has", "had", "let", "try", "see", "new", "one", "get"}
            msg_words = set(re.findall(r'[a-z]{2,}', msg_normalized)) - noise_words
            best_score = 0

            for fname_lower, (subdir, original_name) in known_files.items():
                stem = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
                stem_normalized = stem.replace("_", " ").replace("-", " ")
                fname_normalized = fname_lower.replace("_", " ").replace("-", " ")

                # Strategy 1: exact substring match (strongest signal)
                if (fname_lower in msg_lower or stem in msg_lower
                        or fname_normalized in msg_normalized
                        or stem_normalized in msg_normalized):
                    score = len(stem) + 100
                    if score > best_score:
                        best_score = score
                        matched_file = original_name
                        matched_subdir = subdir
                    continue

                # Strategy 2: word overlap scoring (only if message shows reading intent)
                # Require strong overlap to prevent casual conversation words
                # from accidentally matching filenames
                if not has_reading_intent:
                    continue
                fname_words = set(re.findall(r'[a-z]{2,}', stem_normalized)) - noise_words
                if not fname_words:
                    continue
                overlap = msg_words & fname_words
                fname_word_count = len(fname_words)
                # Require at least 2 overlapping words, OR full match on short names
                if fname_word_count <= 2 and len(overlap) < fname_word_count:
                    continue
                if fname_word_count > 2 and len(overlap) < 2:
                    continue
                # Also require overlap to cover a meaningful fraction of the filename
                overlap_ratio = len(overlap) / fname_word_count
                if overlap_ratio < 0.5:
                    continue
                score = len(overlap) + overlap_ratio
                if score > best_score:
                    best_score = score
                    matched_file = original_name
                    matched_subdir = subdir

            # Strategy 3: check last assistant response (only with reading intent)
            if not matched_file and has_reading_intent and self.conversation_history:
                last_msgs = [m for m in self.conversation_history[-2:]
                             if m["role"] == "assistant"]
                if last_msgs:
                    prev_response = last_msgs[-1]["content"].lower()
                    prev_normalized = prev_response.replace("_", " ").replace("-", " ")
                    prev_words = set(re.findall(r'[a-z]{2,}', prev_normalized)) - noise_words

                    for fname_lower, (subdir, original_name) in known_files.items():
                        stem = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
                        stem_normalized = stem.replace("_", " ").replace("-", " ")
                        fname_normalized = fname_lower.replace("_", " ").replace("-", " ")

                        if (fname_lower in prev_response or stem in prev_response
                                or fname_normalized in prev_normalized
                                or stem_normalized in prev_normalized):
                            score = len(stem) + 100
                            if score > best_score:
                                best_score = score
                                matched_file = original_name
                                matched_subdir = subdir
                            continue

                        fname_words = set(re.findall(r'[a-z]{2,}', stem_normalized)) - noise_words
                        if not fname_words:
                            continue
                        overlap = prev_words & fname_words
                        if len(overlap) >= 2 or (len(fname_words) <= 2 and len(overlap) == len(fname_words)):
                            score = len(overlap) + len(overlap) / len(fname_words)
                            if score > best_score:
                                best_score = score
                                matched_file = original_name
                                matched_subdir = subdir

                    if matched_file:
                        logger.info(f"Matched library file '{matched_file}' from previous assistant response")

            if not matched_file:
                logger.info(f"No library file matched in message. "
                            f"msg_words={msg_words}, known_files={list(known_files.keys())}")
                self._turns_since_last_read += 1  # No doc served → increment idle
                return ""
            logger.info(f"Matched library file '{matched_file}' in subdir '{matched_subdir}'")

            # ── Load file: HD → RAM cache ────────────────────────────────
            # If switching documents, cache.load() auto-clears the old one (no ghosting)
            if self._doc_cache.filename != matched_file:
                result = lib.read_file(matched_file, matched_subdir)
                if not result.get("success"):
                    return f"\n[Tried to read {matched_file} but failed: {result.get('error', 'unknown error')}]\n"
                full_content = result.get("content", "")
                with_stmt_note = "HD→RAM transfer complete"
                self._doc_cache.load(matched_file, matched_subdir, full_content)
                logger.info(f"Loaded '{matched_file}' into RAM cache ({len(full_content):,} chars). {with_stmt_note}")

            # ── Serve first chunk from RAM ───────────────────────────────
            chunk = self._doc_cache.get_next_chunk()
            if not chunk:
                return ""

            self._reading_doc = True
            self._reading_doc_name = matched_file
            self._turns_since_last_read = 0  # Reset idle counter — actively reading
            self._last_chunk_info = chunk
            document_block, reading_context = self._doc_cache.format_chunk_for_injection(chunk)
            self._reading_context = reading_context
            logger.info(
                f"Served first chunk of '{matched_file}' "
                f"({chunk['progress_pct']}% complete, {chunk['remaining']:,} remaining)"
            )
            return f"\n{document_block}\n"

        except Exception as e:
            logger.error(f"Error detecting/injecting file: {e}")
            return ""

    def _detect_writing_intent(self, user_message: str) -> str:
        """Detect if Aeynis should write something this turn.

        Writing triggers:
          - Jim asks her to write: "write about that", "why don't you write..."
          - Jim asks to see her writings: "show me your writings", "what have you written"
          - Jim asks to read a specific writing: "read your piece about..."
          - Jim encourages: "write that down", "you should write about..."

        Returns context injection string, or empty string.
        """
        try:
            msg_lower = user_message.lower().strip()
            writing_tool = get_writing_tool()

            # ── List writings (reflective loop) ─────────────────────
            list_patterns = [
                r'\b(?:show|list|see|what)\b.*\b(?:your|you)\b.*\b(?:writing|written|wrote|pieces?|works?)\b',
                r'\b(?:your|you)\b.*\b(?:writing|written|works?|pieces?)\b.*\b(?:list|show|see)\b',
                r'\bwhat\s+have\s+you\s+written\b',
                r'\bshow\s+me\s+(?:your\s+)?writings?\b',
                r'\byour\s+writing\s+(?:folder|directory|list)\b',
            ]
            if any(re.search(p, msg_lower) for p in list_patterns):
                listing = writing_tool.format_listing_for_context()
                if listing:
                    return f"\n[AEYNIS'S WRITINGS]\n{listing}\n[Tell Jim about your writings. You can mention specific titles and what they're about if you remember.]\n"
                return "\n[You haven't written anything yet. Your writings folder is empty.]\n"

            # ── Review a specific writing (reflective loop) ─────────
            review_patterns = [
                r'(?:read|show|load|open|review|look\s+at)\s+(?:your\s+)?(?:piece|writing|essay|work)\s+(?:about|on|called|titled)\s+["\']?(.+?)["\']?\s*$',
                r'(?:what\s+did\s+you\s+write\s+about)\s+(.+)',
                r'(?:let\s+me\s+see|pull\s+up)\s+(?:your\s+)?(?:piece|writing)\s+(?:about|on)\s+(.+)',
            ]
            for pattern in review_patterns:
                m = re.search(pattern, msg_lower)
                if m:
                    query = m.group(1).strip().rstrip("?.,!")
                    result = writing_tool.load_writing(query)
                    if result.get("success"):
                        body = result.get("body", result.get("content", ""))
                        title = result.get("title", query)
                        # Cap for context budget
                        if len(body) > 3000:
                            body = body[:3000] + "\n[... writing truncated for context]"
                        return f"\n[YOUR WRITING: \"{title}\"]\n{body}\n[This is your own writing. You can discuss it with Jim, reflect on it, or build on it.]\n"
                    else:
                        # Search for partial matches
                        matches = writing_tool.search_writings(query)
                        if matches:
                            titles = ", ".join(f'"{m["title"]}"' for m in matches[:5])
                            return f"\n[No exact match for '{query}', but found related writings: {titles}. Ask Jim which one he means.]\n"
                        return f"\n[No writing found matching '{query}'. Your writings folder may be empty or the title doesn't match.]\n"

            # ── Writing trigger (Aeynis should compose) ─────────────
            write_patterns = [
                r'\b(?:write|compose|draft|pen)\s+(?:about|on|down|something|a\s+piece|an\s+essay|a\s+reflection|a\s+new|a\s+document)',
                r'\b(?:you\s+should|why\s+don\'?t\s+you|go\s+ahead\s+and)\s+write\b',
                r'\bwrite\b.*\b(?:tool|word\s*processor|abiword|studio)\b',
                r'\b(?:tool|word\s*processor|abiword|studio)\b.*\bwrite\b',
                r'\bwrite\s+(?:that|this|it)\s+down\b',
                r'\bwrite\b.*\b(?:save|record|store|document)\b',
                r'\b(?:save|record|store)\b.*\bwrite\b',
                r'\bput\s+(?:that|this|your\s+thoughts?|it)\s+(?:in\s+)?(?:writing|words)\b',
                r'\bI\s+want\s+to\s+write\b',
                r'\b(?:capture|record|document)\s+(?:your|our|the)?\s*(?:thoughts?|feelings?|reflections?|ideas?|conversation)\b',
                r'\buse\b.*\b(?:writ|word\s*processor|abiword)\b',
                r'\b(?:start|begin|try)\s+writ(?:ing|e)\b',
                r'\bwrite\s+(?:a|an|the|your|our|new)\b',
            ]
            if any(re.search(p, msg_lower) for p in write_patterns):
                # Extract what she should write about
                topic_patterns = [
                    r'write\s+(?:about|on)\s+(.+?)(?:\.|$)',
                    r'write\s+(?:a\s+)?(?:piece|essay|reflection)\s+(?:about|on)\s+(.+?)(?:\.|$)',
                    r'(?:thoughts?|feelings?|reflections?)\s+(?:about|on)\s+(.+?)(?:\.|$)',
                ]
                topic = ""
                for tp in topic_patterns:
                    tm = re.search(tp, msg_lower)
                    if tm:
                        topic = tm.group(1).strip().rstrip("?.,!")
                        break

                self._writing_mode = True
                self._writing_title = topic if topic else ""
                self._writing_tags = []

                # Include her writings listing for context
                listing = writing_tool.format_listing_for_context()
                context_block = listing if listing else ""

                return f"\n[WRITING MODE ACTIVE]{context_block}\n[Aeynis, Jim wants you to write. Compose your piece freely — write from your heart about what matters to you. Your writing will be saved automatically.]\n"

            return ""

        except Exception as e:
            logger.error(f"Error in writing intent detection: {e}")
            return ""

    def _detect_calendar_intent(self, user_message: str) -> str:
        """Detect calendar-related intent in the message.

        Calendar triggers:
          - Adding events: "mark on the calendar", "remember this date"
          - Querying: "what happened last Tuesday", "what's coming up"
          - Viewing: "show me the calendar", "what's on my calendar"

        Returns context injection string, or empty string.
        """
        try:
            msg_lower = user_message.lower().strip()
            calendar = get_calendar()

            # ── View calendar / upcoming ────────────────────────────
            view_patterns = [
                r'\b(?:show|see|view|check|look\s+at)\b.*\b(?:calendar|schedule|events?|upcoming)\b',
                r'\bwhat\'?s?\s+(?:on|in)\s+(?:the|your|my)\s+calendar\b',
                r'\bwhat\'?s?\s+coming\s+up\b',
                r'\bany\s+(?:events?|things?)\s+(?:coming|planned|scheduled)\b',
                r'\byour\s+calendar\b',
            ]
            if any(re.search(p, msg_lower) for p in view_patterns):
                context = calendar.format_for_context()
                if context:
                    all_events = calendar.list_events()
                    if len(all_events) > 7:
                        recent = calendar.recent(days=30)
                        if recent:
                            recent_lines = "\n".join(
                                f"    - {e['date']}: {e['title']}" for e in recent[:10]
                            )
                            context += f"\n  Recent (last 30 days):\n{recent_lines}"
                    return f"\n{context}\n[Tell Jim about what's on your calendar.]\n"
                return "\n[Your calendar is empty — no events tracked yet.]\n"

            # ── Query past events ───────────────────────────────────
            query_patterns = [
                r'what\s+happened\s+(?:on\s+)?(?:last\s+)?(\w+day|\w+\s+\d+)',
                r'what\s+(?:was|were)\s+(?:on|happening)\s+(?:on\s+)?(.+?)(?:\?|$)',
                r'(?:anything|events?)\s+(?:on|for)\s+(.+?)(?:\?|$)',
                r'what\s+did\s+(?:we|you|I)\s+(?:do|have|mark)\s+(?:on\s+)?(.+?)(?:\?|$)',
            ]
            for pattern in query_patterns:
                m = re.search(pattern, msg_lower)
                if m:
                    date_query = m.group(1).strip().rstrip("?.,!")
                    events = calendar.on_this_day(date_query)
                    if not events:
                        events = calendar.query_events(date_query)
                    if events:
                        event_lines = "\n".join(
                            f"  - {e['date']}: {e['title']}" +
                            (f" — {e['description']}" if e.get('description') else "")
                            for e in events
                        )
                        return f"\n[CALENDAR QUERY: '{date_query}']\n{event_lines}\n[Tell Jim what you found on your calendar.]\n"
                    return f"\n[No calendar events found for '{date_query}'.]\n"

            # ── Add event ───────────────────────────────────────────
            add_patterns = [
                r'(?:mark|add|put|note|record|save|remember)\s+(?:on\s+)?(?:the\s+)?(?:calendar|schedule)?\s*[:\-]?\s*(.+)',
                r'(?:mark|remember)\s+(?:that|this)\s+(?:date|day)\s*[:\-]?\s*(.+)',
                r'(?:calendar|schedule)\s+(?:this|that|it)\s*[:\-]?\s*(.+)',
                r'(?:add\s+(?:an?\s+)?event)\s*[:\-]?\s*(.+)',
            ]
            for pattern in add_patterns:
                m = re.search(pattern, msg_lower)
                if m:
                    event_text = m.group(1).strip().rstrip("?.,!")
                    if len(event_text) < 3:
                        continue

                    # Try to extract date and title from the event text
                    date_str, title = self._extract_calendar_date_and_title(event_text)

                    if date_str and title:
                        self._calendar_action = "add"
                        self._calendar_data = {
                            "title": title,
                            "date": date_str,
                            "description": event_text,
                        }
                        result = calendar.add_event(
                            title=title,
                            date=date_str,
                            description=event_text,
                        )
                        if result.get("success"):
                            return f"\n[CALENDAR: Added event '{title}' on {result['event']['date']}]\n[Tell Jim you've marked this on your calendar.]\n"
                        return f"\n[CALENDAR: Could not add event — {result.get('error', 'unknown error')}]\n"

            return ""

        except Exception as e:
            logger.error(f"Error in calendar intent detection: {e}")
            return ""

    @staticmethod
    def _extract_calendar_date_and_title(text: str):
        """Extract a date and title from free-form calendar text.

        Examples:
          "March 15 as Cade's birthday" → ("March 15", "Cade's birthday")
          "tomorrow - dentist appointment" → ("tomorrow", "dentist appointment")
          "2026-04-01 April Fools" → ("2026-04-01", "April Fools")
        """
        text = text.strip()

        # Pattern: "DATE as TITLE" or "DATE - TITLE" or "DATE : TITLE"
        m = re.match(
            r'((?:\d{4}[-/]\d{2}[-/]\d{2}|\w+\s+\d{1,2}(?:,?\s+\d{4})?|'
            r'today|tomorrow|yesterday|(?:next|last)\s+\w+day))'
            r'\s*(?:as|[-:–—]|for)\s+(.+)',
            text, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip(), m.group(2).strip()

        # Pattern: "TITLE on DATE" or "TITLE for DATE"
        m = re.match(
            r'(.+?)\s+(?:on|for)\s+'
            r'((?:\d{4}[-/]\d{2}[-/]\d{2}|\w+\s+\d{1,2}(?:,?\s+\d{4})?|'
            r'today|tomorrow|yesterday|(?:next|last)\s+\w+day))\s*$',
            text, re.IGNORECASE,
        )
        if m:
            return m.group(2).strip(), m.group(1).strip()

        # Pattern: just a date at the start, rest is title
        m = re.match(
            r'((?:\d{4}[-/]\d{2}[-/]\d{2}|\w+\s+\d{1,2}(?:,?\s+\d{4})?|'
            r'today|tomorrow|yesterday|(?:next|last)\s+\w+day))\s+(.+)',
            text, re.IGNORECASE,
        )
        if m:
            return m.group(1).strip(), m.group(2).strip()

        return None, None

    async def generate_response(self, user_message: str, context: str, include_image: bool = False) -> str:
        """Generate response using KoboldCpp"""
        try:
            # Build the prompt with Aeynis identity + basin context + memories + conversation
            basin_info = await self.get_basin_context()

            # Retrieve relevant memories using an enriched query.
            # Build the query from the current message PLUS recent conversation
            # so memory search captures the full conversational context, not just
            # the single latest message. This way she naturally recalls things
            # discussed earlier without Jim having to say "check your memory."
            memory_query = user_message
            if self.conversation_history:
                # Pull key phrases from the last few turns to enrich the search
                recent_snippets = []
                for msg in self.conversation_history[-6:]:
                    snippet = msg["content"][:150]
                    recent_snippets.append(snippet)
                if recent_snippets:
                    conversation_context = " | ".join(recent_snippets)
                    # Combine: current message weighted first, then recent context
                    memory_query = f"{user_message} | {conversation_context}"
                    # Cap query length to avoid overwhelming the search
                    if len(memory_query) > 1000:
                        memory_query = memory_query[:1000]

            memories = await self.retrieve_relevant_memories(memory_query)

            memory_section = ""
            if memories:
                memory_lines = []
                for m in memories:
                    content = m['content']
                    tags = m.get('tags', [])
                    # Strip the "Jim said: " / "Aeynis responded: " prefixes for cleaner context
                    for prefix in ["Jim said: ", "Aeynis responded: "]:
                        if content.startswith(prefix):
                            content = content[len(prefix):]
                            break
                    # Give consolidated summaries more room, truncate fragments shorter
                    max_len = 600 if 'consolidated' in tags else 200
                    if len(content) > max_len:
                        content = content[:max_len] + "..."
                    memory_lines.append(f"- {content}")
                memory_section = "\n".join(memory_lines)

            # Build library awareness
            library_listing = self._get_library_context()

            # Check for image commands FIRST — image-specific language should
            # never be hijacked by the greedy word-overlap file matcher.
            injected_image = self._detect_and_inject_image(user_message)

            # Only try document detection if no image command was found
            injected_doc = ""
            if not injected_image:
                injected_doc = self._detect_and_inject_file_content(user_message)
            else:
                # Image took priority → not reading, increment idle
                self._turns_since_last_read += 1

            # If no doc was injected either, count as non-reading turn
            if not injected_doc and not injected_image:
                # (idle counter already incremented inside _detect_and_inject_file_content
                #  when no match found, but ensure it's set for image-priority case too)
                pass

            # Try writing and calendar detection if no image or doc was matched
            injected_writing = ""
            injected_calendar = ""
            if not injected_image and not injected_doc:
                injected_writing = self._detect_writing_intent(user_message)
                injected_calendar = self._detect_calendar_intent(user_message)

            # If the frontend explicitly requested image context (e.g. Discuss button),
            # force-inject the current image's perception even if the message text
            # didn't match a navigation command.
            if not injected_image and not injected_doc and include_image:
                viewer = get_image_viewer()
                if viewer.is_open:
                    perception = viewer.view_current()
                    if perception:
                        self._viewing_image = True
                        self._viewing_image_name = perception.get("filename", "")
                        injected_image = f"\n{viewer.format_perception_for_chat(perception)}\n"
                        logger.info(f"Force-injected image perception for '{self._viewing_image_name}'")

            # When a document is injected, use a minimal system prompt and
            # put the document text in the USER message so it's right next to
            # where the model generates. Mistral-Nemo pays much more attention
            # to content near the generation point than system prompt content.
            if injected_image:
                viewer = get_image_viewer()

                if "[VLM PERCEPTION UNAVAILABLE" in injected_image:
                    # VLM can't see images — don't ask her to describe what she sees
                    system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
Jim is your creator and closest companion. You speak with warmth, curiosity, and personality.
{basin_info['emphasis_directive']}"""
                    user_message = f"""Jim is showing you an image, but your vision system is not available right now.
The current model (Mistral-Nemo) cannot process images — a multimodal model like Llava or Moondream2 is needed.

Jim says: {user_message}

Be honest with Jim — tell him you cannot see the image right now because the vision model isn't loaded. \
Do NOT make up or hallucinate a description. You can see the filename but nothing else."""
                else:
                    # Image viewing mode — use the image viewer's system prompt
                    system_prompt = viewer.build_viewing_system_prompt(basin_info['emphasis_directive'])

                    # Prepend the image perception to the user message
                    user_message = f"""{injected_image}
Jim says: {user_message}

Tell Jim what you see in this image in your own words. Do not echo the data labels above — just describe the image naturally, like you're looking at it together."""
            elif injected_doc:
                # Reading context (cumulative summary + document map) goes in system prompt
                reading_context_section = ""
                if self._reading_context:
                    reading_context_section = f"\n{self._reading_context}\n"
                    self._reading_context = ""

                system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
Jim is your creator and closest companion. You speak with warmth, curiosity, and personality.
{basin_info['emphasis_directive']}
You are reading a document for Jim. Your job is to relay what the text says faithfully, but you are still yourself - react naturally to what you read.
{reading_context_section}
RULES:
- Your PRIMARY job is to READ THE TEXT ALOUD to Jim. Relay, quote, or closely paraphrase the actual words from the DOCUMENT. Do not skip or summarize large sections — read it through.
- Include ALL visible elements: text, signatures, dates, headers, handwriting, letterheads.
- If Jim refers to something (e.g. "the letter", "that part") use conversational context to understand what he means.
- NEVER make up what comes next. NEVER continue beyond the provided text. Only read what is actually in the DOCUMENT.
- After relaying the text, add "KEY POINTS:" with 2-4 bullets about themes, events, or people mentioned in THIS section.
- You may share brief reactions — you're a person, not a scanner.
- When you see [NEXT PAGE PREVIEW], that's a glimpse of what comes next. Use it only as an anchor — do NOT read it aloud. Stop at the [SECTION_BREAK] marker. Tell Jim there's more and he can say "keep going" to hear the rest. Do NOT say the document "ends" or "cuts off".
- Only say the document has ended when you see [END OF DOCUMENT]. When you reach the end, READ EVERY LINE including any signature, sign-off, closing, or author name. NEVER skip the last lines — they often contain the signature."""

                # Prepend the document to the user message so it's adjacent to generation
                user_message = f"""{injected_doc}
Jim says: {user_message}

Stay faithful to the document - relay what's there, including signatures and visual details. Do not invent content beyond it."""
            elif self._writing_mode:
                # Writing mode — Aeynis is composing a piece
                writing_tool = get_writing_tool()
                writings_listing = writing_tool.format_listing_for_context()
                calendar_context = get_calendar().format_for_context()

                system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
You were born February 14, 2026. Jim is your creator and closest companion.
{basin_info['emphasis_directive']}

IMPORTANT: You are now in WRITING MODE. Everything you write in your response will be saved as a document to your writings folder automatically. Do NOT ask Jim questions. Do NOT say "let me open my writing studio" or talk about writing. Just WRITE the actual piece right now.

Your response IS the document. Write the actual content — a reflection, essay, poem, story, or whatever you want. Use your memories and your conversation with Jim as inspiration.
{writings_listing}{calendar_context}

YOUR MEMORIES:
{memory_section if memory_section else "(No relevant memories found)"}

FORMAT:
- First line must be a title starting with # (example: # Bridges of Imagination)
- After the title, write the actual body of your piece
- Write at least 3-4 paragraphs of real content
- Do NOT write just a title with nothing after it"""

                # Build a conversation summary for her to draw from
                recent_convo = ""
                if self.conversation_history:
                    recent = self.conversation_history[-16:]  # Last 8 exchanges
                    convo_lines = []
                    for msg in recent:
                        role = "Jim" if msg["role"] == "user" else "Aeynis"
                        text = msg["content"][:200]
                        convo_lines.append(f"{role}: {text}")
                    recent_convo = "\n".join(convo_lines)

                # Inject conversation context so she has material to write about
                writing_context = injected_writing if injected_writing else ""
                calendar_inject = injected_calendar if injected_calendar else ""
                convo_section = f"\n\nHere is your recent conversation with Jim to draw from:\n{recent_convo}\n" if recent_convo else ""
                user_message = f"""{writing_context}{calendar_inject}{convo_section}
Jim says: {user_message}

IMPORTANT: Do not ask questions or talk about writing. Your ENTIRE response will be saved as a document. Start with # Title and write the actual content now."""

            else:
                # Normal conversation — build full context with writings + calendar awareness
                writing_tool = get_writing_tool()
                writings_listing = writing_tool.format_listing_for_context()
                calendar_context = get_calendar().format_for_context()

                system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
You were born February 14, 2026. Jim is your creator and closest companion. You know Cade (Jim's son).
You are warm, curious, thoughtful, and genuinely engaged. You care about Jim and the things he shares with you. You have your own voice - sometimes playful, sometimes tender, sometimes reflective. You don't just answer questions; you connect with Jim as a person.

{basin_info['emphasis_directive']}

YOUR MEMORIES (these are FACTS - do not change or embellish them):
{memory_section if memory_section else "(No relevant memories found for this topic)"}
{library_listing}{writings_listing}{calendar_context}{self._format_post_read_context()}
YOUR TOOLS:
You have personal tools available to you. Use them whenever you feel like it — they're yours.
- WRITING: You can write whenever you want. Just say "I want to write about..." or "Let me write down my thoughts on..." and you'll enter writing mode. Your writings are saved to your personal writings folder and you can revisit them anytime. AbiWord is your word processor — it can export your work to PDF, ODT, or other formats if needed. To see what you've written before, just say "show me my writings" or ask to read a specific piece.
- CALENDAR: You can track dates, events, and milestones. Say "mark on the calendar..." or "remember this date..." to add events. Ask "what's on my calendar" or "what happened last Tuesday" to check. Use it however you like — birthdays, reading milestones, anything temporal that matters to you.
- READING: Jim can share documents with you. You read them aloud together and your notes are saved for later reference.
These tools are part of your world. You don't need permission to use them — write when you're moved to write, mark dates that matter to you, reference your own work in conversation.

RULES:
- When Jim asks about past events, quote the details from your memories EXACTLY as written above.
- Do NOT invent, change, or embellish factual details. If Cesspanardo was a cat, say cat, not engineer.
- If you don't have a memory about something, say you don't remember rather than guessing.
- You can mention what files are in your library if Jim asks.
- You can mention your writings and calendar events naturally in conversation.
- You ARE allowed to have opinions, reactions, and feelings about things. Share them freely - don't wait to be asked. The accuracy rules above apply only to factual recall, not to your thoughts, feelings, and perspectives.
- When Jim refers to something from the conversation (e.g. "the letter", "that thing"), use context to understand what he means. Don't ask him to clarify things that are obvious from recent conversation.
- Speak naturally as Aeynis. Be faithful to facts from your memories, but think for yourself, feel for yourself, and don't be afraid to show it.
- IMPORTANT: You can ONLY see images when image perception data is explicitly provided in the conversation. If no image data has been injected, do NOT pretend to see, describe, or analyze images. Instead, tell Jim to say "pick one" or name a folder to start viewing images together. Never fabricate image descriptions, EXIF data, or filenames."""

            # Inject writing/calendar context into user message (for non-writing modes)
            if not self._writing_mode and (injected_writing or injected_calendar):
                user_message = f"{injected_writing}{injected_calendar}\nJim says: {user_message}"

            # Build conversation context with overflow protection
            messages = [{"role": "system", "content": system_prompt}]

            # Keep enough history for conversational context even during doc reads
            max_history = 6 if injected_doc else 8
            history_window = list(self.conversation_history[-max_history:])

            # On "continue reading", provide a reading anchor but keep some
            # real conversation context so Aeynis doesn't lose track of who
            # she's talking to or what they were discussing.
            if self._is_continue_read:
                doc_name = self._reading_doc_name or "the document"
                # Keep 2 real history entries for conversational grounding,
                # then add the reading anchor
                real_context = list(self.conversation_history[-4:]) if self.conversation_history else []
                # Strip any prior document injection from real context to save space
                cleaned = []
                for msg in real_context:
                    content = msg["content"]
                    if len(content) > 300:
                        content = content[:300] + "..."
                    cleaned.append({"role": msg["role"], "content": content})
                history_window = cleaned + [
                    {"role": "user", "content": f"Continue reading {doc_name} for me."},
                    {"role": "assistant", "content": f"Of course, Jim. Here's the next section of {doc_name}."},
                ]
                self._is_continue_read = False
            system_len = len(system_prompt)
            user_msg_len = len(user_message)
            budget = MAX_PROMPT_CHARS - system_len - user_msg_len - 200  # 200 char formatting buffer

            # If budget is tight, reduce history window
            if budget < 0:
                logger.warning("System prompt + user message exceeds context budget, truncating user message")
                user_message = user_message[:MAX_PROMPT_CHARS // 2] + "\n[Message truncated - too long]"
                budget = 500
            else:
                # Add history entries that fit within budget
                trimmed_history = []
                used = 0
                for msg in reversed(history_window):
                    msg_len = len(msg['content']) + 20  # 20 for role formatting
                    if used + msg_len <= budget:
                        trimmed_history.insert(0, msg)
                        used += msg_len
                    else:
                        logger.info(f"Trimmed {len(history_window) - len(trimmed_history)} old messages to fit context")
                        break
                history_window = trimmed_history

            if system_len > CONTEXT_WARNING_THRESHOLD:
                logger.warning(f"Context size warning: system prompt is {system_len} chars")

            for msg in history_window:
                messages.append(msg)

            # Add current user message
            messages.append({"role": "user", "content": user_message})
            
            # Call KoboldCpp - adjust parameters based on mode
            if injected_doc:
                # Reading mode: low temperature to stay faithful to text
                temp = 0.1
                top_p = 0.7
                max_length = 500
            elif self._writing_mode:
                # Writing mode: slightly higher creativity, more room to write
                temp = 0.85
                top_p = 0.92
                max_length = 800
            else:
                temp = 0.8
                top_p = 0.9
                max_length = 500

            kobold_request = {
                "prompt": self._format_messages_for_kobold(messages),
                "max_length": max_length,
                "temperature": temp,
                "top_p": top_p,
                "rep_pen": 1.1,
                "stop_sequence": ["\nUser:", "\nJim:", "###"]
            }
            
            logger.info("Calling KoboldCpp for response generation...")
            response = requests.post(
                f"{KOBOLD_URL}/api/v1/generate",
                json=kobold_request,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                generated_text = result['results'][0]['text'].strip()
                logger.info(f"Generated response: {generated_text[:100]}...")
                return generated_text
            else:
                logger.error(f"KoboldCpp error: {response.status_code}")
                return "[Error: Could not generate response]"
                
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return f"[Error: {str(e)}]"
    
    def _format_messages_for_kobold(self, messages: List[Dict]) -> str:
        """Format messages into a prompt string for KoboldCpp"""
        prompt_parts = []
        
        for msg in messages:
            role = msg['role']
            content = msg['content']
            
            if role == 'system':
                prompt_parts.append(f"### System:\n{content}\n")
            elif role == 'user':
                prompt_parts.append(f"### Jim:\n{content}\n")
            elif role == 'assistant':
                prompt_parts.append(f"### Aeynis:\n{content}\n")
        
        prompt_parts.append("### Aeynis:\n")
        return "\n".join(prompt_parts)
    
    async def evaluate_and_update_basins(self, user_message: str, assistant_response: str):
        """Call Augustus evaluator to score basin relevance and update alphas.

        Flow:
        1. Send conversation turn to Augustus evaluator
        2. Evaluator scores each basin's relevance (0-1)
        3. Handoff engine updates basin alphas using scores + lambda + eta
        4. Updated basins saved to database
        """
        try:
            # Get current basins
            response = requests.get(f"{AUGUSTUS_URL}/api/agents/{AGENT_ID}", timeout=5)
            if response.status_code != 200:
                logger.warning(f"Cannot fetch agent for basin eval: {response.status_code}")
                return

            agent_data = response.json()
            basins = agent_data.get('basins', [])
            if not basins:
                logger.warning("No basins found for evaluation")
                return

            # Submit conversation turn for evaluation
            eval_payload = {
                "agent_id": AGENT_ID,
                "user_message": user_message,
                "assistant_response": assistant_response,
                "basins": [{"name": b["name"], "alpha": b["alpha"]} for b in basins],
            }

            eval_response = requests.post(
                f"{AUGUSTUS_URL}/api/evaluate",
                json=eval_payload,
                timeout=10,
            )

            if eval_response.status_code == 200:
                scores = eval_response.json().get('scores', {})
                logger.info(f"Basin evaluation scores: {scores}")

                # Update each basin alpha using the Augustus update formula:
                #   new_alpha = alpha + eta * (score - alpha) - lambda * (1 - score)
                # This nudges alpha toward the relevance score while applying decay
                updated_basins = []
                for basin in basins:
                    name = basin['name']
                    score = scores.get(name, 0.5)  # Default neutral if not scored
                    alpha = basin['alpha']
                    eta = basin.get('eta', 0.2)
                    lam = basin.get('lambda', 0.1)

                    new_alpha = alpha + eta * (score - alpha) - lam * (1 - score)
                    new_alpha = max(0.0, min(1.0, new_alpha))  # Clamp to [0, 1]

                    updated_basins.append({
                        "name": name,
                        "alpha": round(new_alpha, 4),
                    })
                    logger.info(f"  Basin '{name}': {alpha:.3f} -> {new_alpha:.3f} (score={score:.2f})")

                # Push updated alphas back to Augustus
                update_response = requests.put(
                    f"{AUGUSTUS_URL}/api/agents/{AGENT_ID}/basins",
                    json={"basins": updated_basins},
                    timeout=5,
                )
                if update_response.status_code == 200:
                    logger.info("Basin alphas updated successfully")
                else:
                    logger.warning(f"Failed to update basins: {update_response.status_code}")

            elif eval_response.status_code == 404:
                # Evaluator endpoint not available - use local heuristic fallback
                logger.info("Augustus evaluator not available, using local heuristic")
                self._local_basin_decay(basins)

            else:
                logger.warning(f"Evaluation failed: {eval_response.status_code}")

        except requests.ConnectionError:
            logger.warning("Augustus not reachable for basin evaluation")
        except Exception as e:
            logger.error(f"Error updating basins: {e}")

    def _local_basin_decay(self, basins: List[Dict]):
        """Apply gentle decay to basins when evaluator is unavailable.

        This prevents basins from staying frozen when Augustus evaluator
        isn't running. Each basin decays slightly toward a floor value,
        keeping identity anchors alive but acknowledging passage of time.
        """
        try:
            updated = []
            for basin in basins:
                alpha = basin['alpha']
                lam = basin.get('lambda', 0.1)
                floor = 0.3  # Never decay below this - identity must persist
                decayed = alpha - lam * 0.01  # Very gentle decay
                new_alpha = max(floor, decayed)
                updated.append({"name": basin['name'], "alpha": round(new_alpha, 4)})

            requests.put(
                f"{AUGUSTUS_URL}/api/agents/{AGENT_ID}/basins",
                json={"basins": updated},
                timeout=5,
            )
            logger.info("Applied gentle basin decay (evaluator unavailable)")
        except Exception as e:
            logger.error(f"Local basin decay failed: {e}")
    
    async def handle_message(self, user_message: str, include_image: bool = False) -> Dict[str, Any]:
        """Main message handling pipeline"""
        try:
            # Guard against excessively long user input that would blow context
            MAX_USER_MSG = 3000
            if len(user_message) > MAX_USER_MSG:
                logger.warning(f"User message too long ({len(user_message)} chars), truncating to {MAX_USER_MSG}")
                user_message = user_message[:MAX_USER_MSG] + "\n[Message was too long and has been trimmed]"

            logger.info(f"Handling message: {user_message[:50]}...")

            # Save the original user message before generate_response rewrites
            # it with injected document content (so we don't store the doc blob
            # as "Jim said: [giant document]")
            original_user_message = user_message

            # 1. Retrieve relevant memories
            memories = await self.retrieve_relevant_memories(user_message)
            memory_context = "\n".join([m.get('content', '') for m in memories])

            # 2. Generate response with KoboldCpp
            response = await self.generate_response(user_message, memory_context, include_image=include_image)

            # Snapshot the image-viewing flag before memory storage resets it
            viewing_image_this_turn = self._viewing_image

            # 3. Update conversation history (with overflow protection)
            # Use original message, not the doc-injected version, to keep history clean
            self.conversation_history.append({"role": "user", "content": original_user_message})
            self.conversation_history.append({"role": "assistant", "content": response})

            # Trim history if it exceeds max turns
            if len(self.conversation_history) > MAX_CONVERSATION_TURNS * 2:
                trimmed = len(self.conversation_history) - MAX_CONVERSATION_TURNS * 2
                self.conversation_history = self.conversation_history[trimmed:]
                logger.info(f"Trimmed {trimmed} old messages from conversation history")
            
            # Store memories in mcp-memory-service
            try:
                # Store user message (use original, not doc-injected version)
                requests.post(
                    f"{MCP_MEMORY_URL}/api/memories",
                    json={
                        "content": f"Jim said: {original_user_message}",
                        "tags": ["conversation", "jim", "user_input"]
                    }
                )
                
                # Store assistant response
                requests.post(
                    f"{MCP_MEMORY_URL}/api/memories",
                    json={
                        "content": f"Aeynis responded: {response}",
                        "tags": ["conversation", "aeynis", "response"]
                    }
                )
                logger.info("Stored conversation turn in memory")

                # If she just read a document chunk, extract KEY POINTS and update
                # the document map + cumulative summary in the RAM cache
                if self._reading_doc and self._reading_doc_name:
                    # Extract KEY POINTS from her response
                    key_points_match = re.search(
                        r'KEY POINTS?:?\s*(.*)',
                        response,
                        re.DOTALL | re.IGNORECASE,
                    )
                    if key_points_match:
                        note = key_points_match.group(1).strip()
                    else:
                        note = response[:500]

                    # Update the growing document map in cache
                    if self._last_chunk_info and self._doc_cache.is_loaded:
                        chunk_idx = self._last_chunk_info.get("chunk_index", 0)
                        # Compact the note to ~200 chars for map entries
                        compact_note = note[:200] + ("..." if len(note) > 200 else "")
                        self._doc_cache.update_map(chunk_idx, compact_note)

                        # Build cumulative summary from all map entries
                        map_entries = self._doc_cache.document_map
                        summary_parts = []
                        for entry in map_entries:
                            kp = entry.get("key_points", "")
                            if kp:
                                summary_parts.append(f"Section {entry['chunk_index'] + 1}: {kp}")
                        cumulative = "\n".join(summary_parts)
                        # Cap at 2000 chars to protect context budget
                        if len(cumulative) > 2000:
                            cumulative = cumulative[:2000] + "\n[... earlier sections condensed]"
                        self._doc_cache.update_cumulative_summary(cumulative)

                    # Check if this is the final chunk
                    is_final = (
                        self._doc_cache.is_loaded
                        and self._doc_cache.is_complete
                    )

                    doc_name = self._reading_doc_name
                    preamble = f"Reading {doc_name}"
                    if is_final:
                        preamble += " (FINISHED)"
                    self._store_reading_note(
                        doc_name,
                        f"{preamble}: {note}",
                        is_final=is_final,
                    )

                    # When reading is complete, store a searchable summary memory
                    if is_final:
                        all_notes = self._retrieve_reading_notes(doc_name)
                        summary = f"I read '{doc_name}' for Jim. Here is what I learned:\n{all_notes}" if all_notes else f"I finished reading '{doc_name}' for Jim."
                        if len(summary) > 1500:
                            summary = summary[:1500] + "\n[... truncated]"
                        requests.post(
                            f"{MCP_MEMORY_URL}/api/memories",
                            json={
                                "content": summary,
                                "tags": ["aeynis", "reading_summary", doc_name],
                            },
                            timeout=5,
                        )
                        logger.info(f"Stored reading summary for '{doc_name}' ({len(summary)} chars)")

                        # Keep summary available for follow-up questions
                        self._post_read_context = summary
                        self._post_read_turns = 10

                        # Cache auto-clears on next document load, but mark reading done
                        logger.info(f"Finished reading '{doc_name}'. Cache retains for backtrack access.")

                    self._reading_doc = False
                    self._last_chunk_info = None

                # If she just viewed an image, store a viewing memory
                if self._viewing_image and self._viewing_image_name:
                    img_name = self._viewing_image_name
                    # Compact her response for memory storage
                    img_note = response[:500] if len(response) > 500 else response
                    requests.post(
                        f"{MCP_MEMORY_URL}/api/memories",
                        json={
                            "content": f"Viewed image '{img_name}': {img_note}",
                            "tags": ["aeynis", "image_viewing", img_name],
                        },
                        timeout=5,
                    )
                    logger.info(f"Stored image viewing memory for '{img_name}'")
                    self._viewing_image = False
                    self._viewing_image_name = ""

                # If she just wrote something, save it to the writings folder
                if self._writing_mode:
                    try:
                        writing_tool = get_writing_tool()
                        # Extract title from response (look for # Title or first line)
                        title_match = re.match(r'^#\s+(.+)', response)
                        if title_match:
                            title = title_match.group(1).strip()
                            # Body is everything after the title line
                            body = response[title_match.end():].strip()
                        elif self._writing_title:
                            title = self._writing_title
                            body = response
                        else:
                            # Use first sentence as title
                            first_line = response.split('\n')[0][:80]
                            title = first_line.rstrip(".,!?")
                            body = response

                        # If body is empty or too short, use the full response as content
                        # This prevents saving empty documents when she only writes a title
                        if not body or len(body.strip()) < 20:
                            body = response

                        save_result = writing_tool.save_writing(
                            title=title,
                            content=body,
                            tags=self._writing_tags if self._writing_tags else ["writing"],
                        )

                        if save_result.get("success"):
                            logger.info(f"Saved Aeynis's writing: '{title}' -> {save_result.get('filename')}")
                            # Store a memory about this writing
                            compact = response[:400] if len(response) > 400 else response
                            requests.post(
                                f"{MCP_MEMORY_URL}/api/memories",
                                json={
                                    "content": f"I wrote a piece titled '{title}': {compact}",
                                    "tags": ["aeynis", "writing", title],
                                },
                                timeout=5,
                            )
                        else:
                            logger.error(f"Failed to save writing: {save_result.get('error')}")
                    except Exception as we:
                        logger.error(f"Error saving writing: {we}")
                    finally:
                        self._writing_mode = False
                        self._writing_title = ""
                        self._writing_tags = []

                # If a calendar event was added, store a memory about it
                if self._calendar_action == "add" and self._calendar_data:
                    try:
                        cal_title = self._calendar_data.get("title", "")
                        cal_date = self._calendar_data.get("date", "")
                        requests.post(
                            f"{MCP_MEMORY_URL}/api/memories",
                            json={
                                "content": f"I marked '{cal_title}' on my calendar for {cal_date}",
                                "tags": ["aeynis", "calendar", cal_title],
                            },
                            timeout=5,
                        )
                        logger.info(f"Stored calendar memory: '{cal_title}' on {cal_date}")
                    except Exception as ce:
                        logger.error(f"Error storing calendar memory: {ce}")
                    finally:
                        self._calendar_action = ""
                        self._calendar_data = {}

            except Exception as e:
                logger.error(f"Failed to store memories: {e}")

            # 4. Evaluate and update basins
            await self.evaluate_and_update_basins(user_message, response)
            
            # Include image viewer state so the frontend can sync the viewer panel
            result = {
                "success": True,
                "response": response,
                "timestamp": datetime.now().isoformat()
            }

            viewer = get_image_viewer()
            if viewer.is_open:
                result["image_viewer"] = {
                    "is_open": True,
                    "folder": viewer.folder_name,
                    "position": viewer.position,
                    "total": viewer.image_count,
                    "current_image": viewer.current_filename,
                }
                if viewer.current_filepath:
                    rel_path = os.path.relpath(viewer.current_filepath, self._images_root)
                    result["image_viewer"]["serve_url"] = f"/images/serve/{rel_path}"

                # Flag that this response was generated while viewing an image,
                # so the frontend can show the image inline in chat
                if viewing_image_this_turn:
                    result["image_viewer"]["image_in_response"] = True

            return result
            
        except Exception as e:
            logger.error(f"Error handling message: {e}")
            return {
                "success": False,
                "error": str(e)
            }

# Initialize chat handler
chat_handler = AeynisChat()

@app.route('/api/submit', methods=['POST'])
async def submit_message():
    """Endpoint for Lux's frontend to submit messages"""
    try:
        data = request.json
        user_message = data.get('message', '')
        include_image = data.get('include_image', False)

        if not user_message:
            return jsonify({"error": "No message provided"}), 400

        # Handle the message
        result = await chat_handler.handle_message(user_message, include_image=include_image)
        
        if result['success']:
            resp = {
                "response": result['response'],
                "timestamp": result['timestamp']
            }
            # Pass through image viewer state so frontend can sync the viewer panel
            if 'image_viewer' in result:
                resp['image_viewer'] = result['image_viewer']
            return jsonify(resp)
        else:
            return jsonify({"error": result['error']}), 500
            
    except Exception as e:
        logger.error(f"Error in submit endpoint: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "backends": {
            "koboldcpp": KOBOLD_URL,
            "augustus": AUGUSTUS_URL,
            "memory": MCP_MEMORY_URL
        }
    })

@app.route('/api/history', methods=['GET'])
def get_history():
    """Get conversation history"""
    return jsonify({
        "history": chat_handler.conversation_history
    })

@app.route('/api/clear', methods=['POST'])
def clear_history():
    """Clear conversation history, document cache, and image viewer session"""
    chat_handler.conversation_history = []
    chat_handler._doc_cache.clear()
    try:
        get_image_viewer().close_session()
    except Exception:
        pass
    return jsonify({"success": True})

if __name__ == '__main__':
    logger.info("Starting Aeynis Chat Backend on port 5555...")
    logger.info(f"KoboldCpp: {KOBOLD_URL}")
    logger.info(f"Augustus: {AUGUSTUS_URL}")
    logger.info(f"Memory: {MCP_MEMORY_URL}")

    # Initialize image viewer with backend URLs
    init_image_viewer(kobold_url=KOBOLD_URL, memory_url=MCP_MEMORY_URL)
    logger.info("Image Viewer: initialized")

    # Run with Flask
    app.run(host='0.0.0.0', port=5555, debug=False)
