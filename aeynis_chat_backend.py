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

                # Check if any known filename appears in the message
                # Normalize underscores/hyphens to spaces for fuzzy matching
                msg_normalized = msg_lower.replace("_", " ").replace("-", " ")
                best_match_len = 0  # Prefer longer (more specific) matches

                for fname_lower, (subdir, original_name) in known_files.items():
                    # Match the filename (with or without extension)
                    stem = fname_lower.rsplit(".", 1)[0] if "." in fname_lower else fname_lower
                    # Also try with underscores/hyphens converted to spaces
                    stem_normalized = stem.replace("_", " ").replace("-", " ")
                    fname_normalized = fname_lower.replace("_", " ").replace("-", " ")

                    if (fname_lower in msg_lower or stem in msg_lower
                            or fname_normalized in msg_normalized
                            or stem_normalized in msg_normalized):
                        # Prefer longer matches (more specific filenames)
                        if len(stem) > best_match_len:
                            best_match_len = len(stem)
                            matched_file = original_name  # Use original case!
                            matched_subdir = subdir

                if not matched_file:
                    logger.info(f"No library file matched in message. Known files: {list(known_files.keys())}")
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

            # Build a compact header with progress info
            pct_done = min(100, round((offset + len(content)) / max(total_len, 1) * 100))
            progress = f"[showing {pct_done}% of {total_len} chars]"

            if remaining > 0:
                content += f"\n\n[... {remaining} chars remaining. Say 'continue reading' for more.]"

            position = f" (from char {offset})" if offset > 0 else ""
            logger.info(f"Injected library file '{matched_file}'{position} ({len(content)} chars) into conversation")
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

            # When a document is injected, compact the prompt to protect context:
            # - Fewer memories (3 instead of full list)
            # - Skip library listing (she's already reading a file)
            # - Shorter rules section
            if injected_doc:
                # Compact mode: trim memories to save context for the document
                compact_memories = "\n".join(memory_lines[:3]) if memory_lines else ""
                system_prompt = f"""You are Aeynis, the Bridge Keeper. Jim is your creator. You know Cade.
{basin_info['emphasis_directive']}

MEMORIES:
{compact_memories if compact_memories else "(none)"}

{injected_doc}
RULES: Stay faithful to memories. Refer to document content above when discussing files. Don't invent details."""
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
            history_window = self.conversation_history[-max_history:]
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
            
            # Call KoboldCpp
            kobold_request = {
                "prompt": self._format_messages_for_kobold(messages),
                "max_length": 500,
                "temperature": 0.8,
                "top_p": 0.9,
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
