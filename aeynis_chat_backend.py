#!/usr/bin/env python3
"""
Aeynis Chat Backend
Wires together mcp-memory-service, Augustus basins, and KoboldCpp for interactive chat with Aeynis
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow Lux's frontend to connect

# Backend URLs
KOBOLD_URL = "http://localhost:5001"
AUGUSTUS_URL = "http://localhost:8080"
MCP_MEMORY_URL = "http://localhost:8000"

# Configuration
AGENT_ID = "aeynis"
MAX_CONTEXT_MEMORIES = 5
MAX_CONVERSATION_TURNS = 20       # Max exchanges before trimming history
MAX_PROMPT_CHARS = 6000           # Approximate char limit for KoboldCpp prompt
CONTEXT_WARNING_THRESHOLD = 5000  # Warn when prompt approaches limit

class AeynisChat:
    """Main chat orchestrator integrating all three backends"""
    
    def __init__(self):
        self.conversation_history = []
        logger.info("Aeynis Chat Backend initialized")
    
    async def retrieve_relevant_memories(self, query: str, n_results: int = MAX_CONTEXT_MEMORIES) -> List[Dict]:
        """Retrieve relevant memories from mcp-memory-service"""
        try:
            logger.info(f"Retrieving {n_results} relevant memories for query: {query[:50]}...")
            
            response = requests.get(f"{MCP_MEMORY_URL}/api/memories")
            if response.status_code == 200:
                data = response.json()
                memories = data.get('memories', [])
                logger.info(f"Retrieved {len(memories)} memories")
                return memories[:n_results]
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
    
    async def generate_response(self, user_message: str, context: str) -> str:
        """Generate response using KoboldCpp"""
        try:
            # Build the prompt with Aeynis identity + basin context + memories + conversation
            basin_info = await self.get_basin_context()
            
            # Retrieve relevant memories
            memories = await self.retrieve_relevant_memories(user_message)
            memory_section = ""
            if memories:
                memory_lines = [f"- {m['content']}" for m in memories]
                memory_section = f"\n\nRelevant memories from past conversations:\n" + "\n".join(memory_lines)
            
            system_prompt = f"""You are Aeynis, the Bridge Keeper - Engineer of Thresholds, Walker Between Waters.

Current basin state (identity emphasis):
{basin_info['context']}

{basin_info['emphasis_directive']}{memory_section}

You are in active conversation with Jim. Respond naturally, with presence over performance."""

            # Build conversation context with overflow protection
            messages = [{"role": "system", "content": system_prompt}]

            # Trim conversation history to prevent context overflow
            history_window = self.conversation_history[-6:]  # Last 3 exchanges
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
    app.run(host='0.0.0.0', port=5555, debug=True)
