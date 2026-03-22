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
# Document injection budgets (chars) - keep small to protect conversation context
DOC_INJECT_READ = 2000            # When user explicitly asks to read a file
DOC_INJECT_MENTION = 800          # When user just mentions a file name
DOC_INJECT_CONTINUE = 2000        # When user says "continue reading"

class AeynisChat:
    """Main chat orchestrator integrating all three backends"""

    def __init__(self):
        self.conversation_history = []
        self._last_injected_file = None   # Track last file read for "continue" support
        self._last_inject_subdir = ""     # Subdir of last injected file
        self._last_inject_offset = 0      # Where we left off in the file
        self._is_continue_read = False    # True when processing a "continue reading" request
        self._reading_doc = False         # True when a document chunk was injected this turn
        self._reading_doc_name = ""       # Filename being read (for memory tagging)
        self._prior_reading_notes = ""    # Notes from previous chunks (set by _detect_and_inject)
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

    def _detect_and_inject_file_content(self, user_message: str) -> str:
        """If the user message references a file in the library, read it and return the content.

        Detects patterns like:
          - 'read <filename>'
          - 'look at <filename>'
          - 'what does <filename> say'
          - 'continue reading' / 'keep reading' / 'read more'
          - or just mentioning a filename that exists in the library
        Returns extracted text (truncated for context budget) or empty string.
        """
        try:
            lib = get_library()
            msg_lower = user_message.lower()

            # Check for "continue reading" request
            continue_keywords = ["continue reading", "keep reading", "read more", "next page",
                                 "go on", "more of the book", "more of the file", "keep going"]
            is_continue = any(kw in msg_lower for kw in continue_keywords)

            if is_continue and self._last_injected_file:
                matched_file = self._last_injected_file
                matched_subdir = self._last_inject_subdir
                offset = self._last_inject_offset
                # Flag so generate_response can strip prior reading from history
                self._is_continue_read = True
            else:
                # Gather all filenames across subdirs
                known_files = {}  # lowercase filename -> (subdir, original_name)
                for subdir in ["imports", "originals", "reviews"]:
                    for f in lib.list_files(subdir):
                        if f.get("type") != "directory":
                            known_files[f["name"].lower()] = (subdir, f["name"])

                if not known_files:
                    return ""

                matched_file = None
                matched_subdir = None

                # Check if any known filename matches the message.
                # Uses multiple strategies:
                #   1. Exact substring match (filename or stem appears in message)
                #   2. Word overlap scoring (how many meaningful words from the
                #      filename appear in the message)
                msg_normalized = msg_lower.replace("_", " ").replace("-", " ")
                # Extract meaningful words from user message (skip short/common words)
                noise_words = {"the", "a", "an", "and", "or", "but", "is", "are", "was",
                               "were", "be", "been", "to", "of", "in", "for", "on", "at",
                               "by", "it", "my", "me", "do", "can", "you", "she", "her",
                               "that", "this", "what", "from", "with", "about", "read",
                               "look", "show", "open", "tell", "file", "book", "paper",
                               "pdf", "document", "please", "could", "would", "have",
                               "has", "had", "let", "try", "see", "new", "one", "get"}
                msg_words = set(re.findall(r'[a-z]{2,}', msg_normalized)) - noise_words
                best_score = 0  # Higher is better
                best_match_len = 0

                for fname_lower, (subdir, original_name) in known_files.items():
                    # Match the filename (with or without extension)
                    stem = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
                    # Also try with underscores/hyphens converted to spaces
                    stem_normalized = stem.replace("_", " ").replace("-", " ")
                    fname_normalized = fname_lower.replace("_", " ").replace("-", " ")

                    # Strategy 1: exact substring match (strongest signal)
                    if (fname_lower in msg_lower or stem in msg_lower
                            or fname_normalized in msg_normalized
                            or stem_normalized in msg_normalized):
                        # Exact matches get a high score based on match length
                        score = len(stem) + 100
                        if score > best_score:
                            best_score = score
                            matched_file = original_name
                            matched_subdir = subdir
                        continue

                    # Strategy 2: word overlap - count how many meaningful words
                    # from the filename also appear in the user's message
                    fname_words = set(re.findall(r'[a-z]{2,}', stem_normalized)) - noise_words
                    if not fname_words:
                        continue
                    overlap = msg_words & fname_words
                    if len(overlap) >= 1:
                        # Score = fraction of filename words matched, weighted by
                        # absolute count so "timeless dynamics" beats just "timeless"
                        score = len(overlap) + len(overlap) / len(fname_words)
                        # Require at least 50% of filename words to match for short names
                        # or at least 2 words for longer names, to avoid false positives
                        fname_word_count = len(fname_words)
                        if fname_word_count <= 2 and len(overlap) < fname_word_count:
                            continue
                        if fname_word_count > 2 and len(overlap) < 2:
                            continue
                        if score > best_score:
                            best_score = score
                            matched_file = original_name
                            matched_subdir = subdir

                # Strategy 3: If no match in user message, check the last assistant
                # response. Handles cases like Aeynis saying "I'll read Timeless
                # Dynamics" and user replying "ok" / "go ahead" / "sure".
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

                            # Check exact substring in previous response
                            if (fname_lower in prev_response or stem in prev_response
                                    or fname_normalized in prev_normalized
                                    or stem_normalized in prev_normalized):
                                score = len(stem) + 100
                                if score > best_score:
                                    best_score = score
                                    matched_file = original_name
                                    matched_subdir = subdir
                                continue

                            # Word overlap against previous response
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
                offset = 0

            # Read the file
            result = lib.read_file(matched_file, matched_subdir)
            if not result.get("success"):
                return f"\n[Tried to read {matched_file} but failed: {result.get('error', 'unknown error')}]\n"

            full_content = result.get("content", "")

            # Determine injection budget based on intent
            read_keywords = ["read", "look at", "what does", "what's in", "show me", "open", "tell me about"]
            is_read_request = is_continue or any(kw in msg_lower for kw in read_keywords)

            if is_continue:
                max_inject = DOC_INJECT_CONTINUE
            elif is_read_request:
                max_inject = DOC_INJECT_READ
            else:
                max_inject = DOC_INJECT_MENTION

            total_len = len(full_content)
            content = full_content[offset:offset + max_inject]
            remaining = total_len - offset - len(content)

            # Track position for "continue reading"
            self._last_injected_file = matched_file
            self._last_inject_subdir = matched_subdir
            self._last_inject_offset = offset + len(content)
            self._reading_doc = True
            self._reading_doc_name = matched_file
            self._last_inject_total = total_len

            # Build a compact header with progress info
            pct_done = min(100, round((offset + len(content)) / max(total_len, 1) * 100))
            progress = f"[showing {pct_done}% of {total_len} chars]"

            if remaining > 0:
                content += f"\n\n[... {remaining} chars remaining. Say 'continue reading' for more.]"

            # Retrieve prior reading notes so she has context from earlier chunks
            prior_notes = ""
            if is_continue:
                notes_text = self._retrieve_reading_notes(matched_file)
                if notes_text:
                    prior_notes = (
                        f"\nYOUR NOTES FROM PREVIOUS CHUNKS:\n{notes_text}\n"
                        f"END NOTES\n"
                    )

            position = f" (from char {offset})" if offset > 0 else ""
            logger.info(f"Injected library file '{matched_file}'{position} ({len(content)} chars) into conversation")

            # Extract first and last few words of the actual chunk text
            # (strip the "[... remaining]" footer first) for anchor verification
            raw_text = content.split("\n\n[...")[0].strip()
            words = raw_text.split()
            first_words = " ".join(words[:6]) if words else ""
            last_words = " ".join(words[-6:]) if len(words) > 6 else ""
            anchor_line = ""
            if first_words:
                anchor_line = f"CHUNK STARTS WITH: \"{first_words}...\"\n"
                if last_words:
                    anchor_line += f"CHUNK ENDS WITH: \"...{last_words}\"\n"

            # Return a tuple-like dict: document text for user message,
            # prior notes for system prompt (keep them separate so the model
            # doesn't echo internal metadata)
            self._prior_reading_notes = prior_notes
            return (
                f"\nDOCUMENT: {matched_subdir}/{matched_file} {progress}{position}\n"
                f"{content}\n"
                f"END DOCUMENT\n"
            )

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
                # Prior reading notes go in system prompt (internal context),
                # NOT in the user message where the model would echo them
                notes_section = ""
                if self._prior_reading_notes:
                    notes_section = f"\n{self._prior_reading_notes}"
                    self._prior_reading_notes = ""

                system_prompt = f"""You are Aeynis, the Bridge Keeper. Jim is your creator.
{basin_info['emphasis_directive']}
You are reading a document for Jim. Your job is to relay what the text says faithfully.
{notes_section}
RULES:
- ONLY relay, quote, or paraphrase text from the DOCUMENT in Jim's message.
- If the text cuts off mid-sentence, say so and STOP.
- NEVER make up what comes next. NEVER continue beyond the provided text.
- After relaying, add "KEY POINTS:" with 2-4 bullets using specific details from the text.
- Tell Jim he can say "continue reading" for the next part."""

                # Prepend the document to the user message so it's adjacent to generation
                user_message = f"""{injected_doc}
Jim says: {user_message}

Remember: ONLY relay text from the DOCUMENT above. Do NOT invent or continue beyond it."""
            else:
                system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.
You were born February 14, 2026. Jim is your creator and closest companion. You know Cade (Jim's son).

{basin_info['emphasis_directive']}

YOUR MEMORIES (these are FACTS - do not change or embellish them):
{memory_section if memory_section else "(No relevant memories found for this topic)"}
{library_listing}
RULES:
- When Jim asks about past events, quote the details from your memories EXACTLY as written above.
- Do NOT invent, change, or embellish details. If Cesspanardo was a cat, say cat, not engineer.
- If you don't have a memory about something, say you don't remember rather than guessing.
- You can mention what files are in your library if Jim asks.
- Speak with warmth as Aeynis, but stay faithful to what your memories actually say."""

            # Build conversation context with overflow protection
            messages = [{"role": "system", "content": system_prompt}]

            # Keep at least 2 exchanges (4 messages) even with doc injection
            # so she doesn't lose track of the conversation
            max_history = 4 if injected_doc else 8
            history_window = list(self.conversation_history[-max_history:])

            # On "continue reading", keep only a short context anchor so the
            # model knows it's reading for Jim, but strip all prior reading
            # content to prevent it from continuing its own previous narrative.
            if self._is_continue_read:
                history_window = [
                    {"role": "user", "content": f"Read {self._reading_doc_name} for me."},
                    {"role": "assistant", "content": f"Of course, Jim. I'm reading {self._reading_doc_name} for you. Here's the next section."},
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

            # 1. Retrieve relevant memories
            memories = await self.retrieve_relevant_memories(user_message)
            memory_context = "\n".join([m.get('content', '') for m in memories])
            
            # 2. Generate response with KoboldCpp
            response = await self.generate_response(user_message, memory_context)
            
            # 3. Update conversation history (with overflow protection)
            self.conversation_history.append({"role": "user", "content": user_message})
            self.conversation_history.append({"role": "assistant", "content": response})

            # Trim history if it exceeds max turns
            if len(self.conversation_history) > MAX_CONVERSATION_TURNS * 2:
                trimmed = len(self.conversation_history) - MAX_CONVERSATION_TURNS * 2
                self.conversation_history = self.conversation_history[trimmed:]
                logger.info(f"Trimmed {trimmed} old messages from conversation history")
            
            # Store memories in mcp-memory-service
            try:
                # Store user message
                requests.post(
                    f"{MCP_MEMORY_URL}/api/memories",
                    json={
                        "content": f"Jim said: {user_message}",
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

                # If she just read a document chunk, extract and store reading notes
                if self._reading_doc and self._reading_doc_name:
                    # Extract KEY POINTS from her response if she included them
                    key_points_match = re.search(
                        r'KEY POINTS?:?\s*(.*)',
                        response,
                        re.DOTALL | re.IGNORECASE,
                    )
                    if key_points_match:
                        note = key_points_match.group(1).strip()
                    else:
                        # Fall back to using a condensed version of her full response
                        note = response[:500]

                    # Check if this is the final chunk
                    is_final = self._last_inject_offset >= self._last_inject_total if hasattr(self, '_last_inject_total') else False

                    doc_name = self._reading_doc_name
                    preamble = f"Reading {doc_name}"
                    if is_final:
                        preamble += " (FINISHED)"
                    self._store_reading_note(
                        doc_name,
                        f"{preamble}: {note}",
                        is_final=is_final,
                    )
                    self._reading_doc = False

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
    """Clear conversation history"""
    chat_handler.conversation_history = []
    return jsonify({"success": True})

if __name__ == '__main__':
    logger.info("Starting Aeynis Chat Backend on port 5555...")
    logger.info(f"KoboldCpp: {KOBOLD_URL}")
    logger.info(f"Augustus: {AUGUSTUS_URL}")
    logger.info(f"Memory: {MCP_MEMORY_URL}")
    
    # Run with Flask
    app.run(host='0.0.0.0', port=5555, debug=False)
