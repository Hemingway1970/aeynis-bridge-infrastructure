#!/bin/bash
#
# watchdog.sh - Monitor Aeynis Bridge services and restart any that have crashed
#
# Install via cron:
#   * * * * * ~/bridge/watchdog.sh >> ~/bridge/logs/watchdog.log 2>&1
#
# Only restarts services that were previously started (have a PID file).
# Does NOT start services from scratch - use start_aeynis.sh for that.
#

BRIDGE_DIR="$HOME/bridge"
LOG_DIR="$BRIDGE_DIR/logs"
PID_DIR="$BRIDGE_DIR/pids"

# Exit silently if bridge was never started (no PID dir)
[ -d "$PID_DIR" ] || exit 0

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

restart_service() {
    local name="$1"
    local pid_file="$PID_DIR/${name}.pid"
    local port="$2"
    local start_cmd="$3"
    local log_file="$LOG_DIR/${name}.log"

    # Only manage services that were previously started
    [ -f "$pid_file" ] || return 0

    # Check if process is still alive
    local old_pid
    old_pid=$(cat "$pid_file" 2>/dev/null)
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        return 0  # Still running, nothing to do
    fi

    # Double-check via port - maybe it restarted with a new PID
    if [ -n "$port" ]; then
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            # Port is occupied - update PID file with actual process
            local actual_pid
            actual_pid=$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | head -1)
            if [ -n "$actual_pid" ]; then
                echo "$actual_pid" > "$pid_file"
            fi
            return 0
        fi
    fi

    # Service is dead - restart it
    echo "[$TIMESTAMP] RESTART: $name (was PID $old_pid, port $port)"
    eval "$start_cmd"
    local new_pid=$!
    echo "$new_pid" > "$pid_file"
    echo "[$TIMESTAMP] STARTED: $name (new PID $new_pid)"
}

# --- Service definitions ---

# KoboldCpp (port 5001)
restart_service "koboldcpp" 5001 \
    "cd $HOME/koboldcpp && nohup python3 koboldcpp.py Mistral-Nemo-Instruct-2407-Q4_K_M.gguf --usecublas --gpulayers 40 >> $LOG_DIR/koboldcpp.log 2>&1 &"

# Augustus (port 8080)
restart_service "augustus" 8080 \
    "cd $BRIDGE_DIR/Augustus/backend && nohup bash -c 'source $BRIDGE_DIR/Augustus/backend/augustus_venv/bin/activate && python3 -m augustus.main' >> $LOG_DIR/augustus.log 2>&1 &"

# mcp-memory-service (port 8000)
restart_service "mcp_memory" 8000 \
    "cd $BRIDGE_DIR/mcp-memory-service && MCP_ALLOW_ANONYMOUS_ACCESS=true nohup python3 run_server.py >> $LOG_DIR/mcp_memory.log 2>&1 &"

# Aeynis Chat Backend (port 5555)
restart_service "aeynis_backend" 5555 \
    "cd $BRIDGE_DIR && nohup python3 aeynis_chat_backend.py >> $LOG_DIR/aeynis_backend.log 2>&1 &"

# Memory consolidator (no port - just check PID)
restart_service "consolidator" "" \
    "cd $BRIDGE_DIR && nohup python3 memory_consolidator.py --watch --interval 30 >> $LOG_DIR/consolidator.log 2>&1 &"
