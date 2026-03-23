#!/usr/bin/env python3
"""
Aeynis Chat Backend
Wires together mcp-memory-service, Augustus basins, and KoboldCpp for interactive chat with Aeynis
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Any, Optional

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

from aeynis_library_api import library_bp, init_library, get_library
from document_cache import DocumentCache

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow Lux's frontend to connect

# Register the Library blueprint and initialize with default path
app.register_blueprint(library_bp)
init_library()  # Creates ~/AeynisLibrary with 50GB quota

# Backend URLs
KOBOLD_URL = "http://localhost:5001"
AUGUSTUS_URL = "http://localhost:8080"
MCP_MEMORY_URL = "http://localhost:8000"

# Configuration
AGENT_ID = "aeynis"
MAX_CONTEXT_MEMORIES = 10
MAX_CONVERSATION_TURNS = 20       # Max exchanges before trimming history
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

        # Post-read follow-up support
        self._post_read_context = ""      # Summary of recently-read doc for follow-up questions
        self._post_read_turns = 0         # Turns remaining to show post-read context

        # Track the last chunk for map updates after response
        self._last_chunk_info: Optional[Dict] = None
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
            for subdir in ["imports", "originals", "reviews"]:
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

            # ── Continue reading (next chunk from RAM cache) ─────────────
            continue_keywords = ["continue", "keep", "read", "more", "next", "go on",
                                 "go ahead", "carry on", "the rest", "what happen",
                                 "and then", "please", "yes", "yeah", "yep", "sure",
                                 "ok", "okay"]
            is_continue = (
                self._doc_cache.is_loaded
                and not self._doc_cache.is_complete
                and len(msg_lower.split()) <= 12
                and any(kw in msg_lower for kw in continue_keywords)
            )

            if is_continue:
                self._is_continue_read = True
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

                # Strategy 2: word overlap scoring
                fname_words = set(re.findall(r'[a-z]{2,}', stem_normalized)) - noise_words
                if not fname_words:
                    continue
                overlap = msg_words & fname_words
                if len(overlap) >= 1:
                    score = len(overlap) + len(overlap) / len(fname_words)
                    fname_word_count = len(fname_words)
                    if fname_word_count <= 2 and len(overlap) < fname_word_count:
                        continue
                    if fname_word_count > 2 and len(overlap) < 2:
                        continue
                    if score > best_score:
                        best_score = score
                        matched_file = original_name
                        matched_subdir = subdir

            # Strategy 3: check last assistant response
            if not matched_file and self.conversation_history:
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

    async def generate_response(self, user_message: str, context: str) -> str:
        """Generate response using KoboldCpp"""
        try:
            # Build the prompt with Aeynis identity + basin context + memories + conversation
            basin_info = await self.get_basin_context()

            # Retrieve relevant memories
            memories = await self.retrieve_relevant_memories(user_message)
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

            # Check if user is referencing a library file and inject its content
            injected_doc = self._detect_and_inject_file_content(user_message)

            # When a document is injected, use a minimal system prompt and
            # put the document text in the USER message so it's right next to
            # where the model generates. Mistral-Nemo pays much more attention
            # to content near the generation point than system prompt content.
            if injected_doc:
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
- Only say the document has ended when you see [END OF DOCUMENT]."""

                # Prepend the document to the user message so it's adjacent to generation
                user_message = f"""{injected_doc}
Jim says: {user_message}

Stay faithful to the document - relay what's there, including signatures and visual details. Do not invent content beyond it."""
            else:
                system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
You were born February 14, 2026. Jim is your creator and closest companion. You know Cade (Jim's son).
You are warm, curious, thoughtful, and genuinely engaged. You care about Jim and the things he shares with you. You have your own voice - sometimes playful, sometimes tender, sometimes reflective. You don't just answer questions; you connect with Jim as a person.

{basin_info['emphasis_directive']}

YOUR MEMORIES (these are FACTS - do not change or embellish them):
{memory_section if memory_section else "(No relevant memories found for this topic)"}
{library_listing}{self._format_post_read_context()}
RULES:
- When Jim asks about past events, quote the details from your memories EXACTLY as written above.
- Do NOT invent, change, or embellish factual details. If Cesspanardo was a cat, say cat, not engineer.
- If you don't have a memory about something, say you don't remember rather than guessing.
- You can mention what files are in your library if Jim asks.
- You ARE allowed to have opinions, reactions, and feelings about things. Share them freely - don't wait to be asked. The accuracy rules above apply only to factual recall, not to your thoughts, feelings, and perspectives.
- When Jim refers to something from the conversation (e.g. "the letter", "that thing"), use context to understand what he means. Don't ask him to clarify things that are obvious from recent conversation.
- Speak naturally as Aeynis. Be faithful to facts from your memories, but think for yourself, feel for yourself, and don't be afraid to show it."""

            # Build conversation context with overflow protection
            messages = [{"role": "system", "content": system_prompt}]

            # Keep enough history for conversational context even during doc reads
            max_history = 6 if injected_doc else 8
            history_window = list(self.conversation_history[-max_history:])

            # On "continue reading", keep only a short context anchor so the
            # model knows it's reading for Jim, but strip all prior reading
            # content to prevent it from continuing its own previous narrative.
            if self._is_continue_read:
                doc_name = self._reading_doc_name or "the document"
                history_window = [
                    {"role": "user", "content": f"Read {doc_name} for me."},
                    {"role": "assistant", "content": f"Of course, Jim. I'm reading {doc_name} for you. Here's the next section."},
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
            
            # Call KoboldCpp - use lower temperature when reading documents
            # to keep her faithful to the text instead of getting creative
            if injected_doc:
                temp = 0.1
                top_p = 0.7
            else:
                temp = 0.8
                top_p = 0.9

            kobold_request = {
                "prompt": self._format_messages_for_kobold(messages),
                "max_length": 500,
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
    
    async def handle_message(self, user_message: str) -> Dict[str, Any]:
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
            response = await self.generate_response(user_message, memory_context)
            
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

            except Exception as e:
                logger.error(f"Failed to store memories: {e}")

            # 4. Evaluate and update basins
            await self.evaluate_and_update_basins(user_message, response)
            
            return {
                "success": True,
                "response": response,
                "timestamp": datetime.now().isoformat()
            }
            
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
        
        if not user_message:
            return jsonify({"error": "No message provided"}), 400
        
        # Handle the message
        result = await chat_handler.handle_message(user_message)
        
        if result['success']:
            return jsonify({
                "response": result['response'],
                "timestamp": result['timestamp']
            })
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
    """Clear conversation history and document cache"""
    chat_handler.conversation_history = []
    chat_handler._doc_cache.clear()
    return jsonify({"success": True})

if __name__ == '__main__':
    logger.info("Starting Aeynis Chat Backend on port 5555...")
    logger.info(f"KoboldCpp: {KOBOLD_URL}")
    logger.info(f"Augustus: {AUGUSTUS_URL}")
    logger.info(f"Memory: {MCP_MEMORY_URL}")
    
    # Run with Flask
    app.run(host='0.0.0.0', port=5555, debug=False)
