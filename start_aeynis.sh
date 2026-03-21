#!/bin/bash
#
# start_aeynis.sh - Start all Aeynis Bridge services
#
# Usage: ~/bridge/start_aeynis.sh
#
# Starts services in dependency order:
#   1. KoboldCpp (inference engine)
#   2. Augustus (basin identity tracking)
#   3. mcp-memory-service (memory storage)
#   4. Aeynis Chat Backend (orchestrator)
#

set -e

BRIDGE_DIR="$HOME/bridge"
LOG_DIR="$BRIDGE_DIR/logs"
PID_DIR="$BRIDGE_DIR/pids"

# Create directories
mkdir -p "$LOG_DIR" "$PID_DIR"

echo "========================================"
echo "  Starting Aeynis Bridge Infrastructure"
echo "  $(date)"
echo "========================================"

# Check if a service is already running on a port
check_port() {
    local port=$1
    local name=$2
    if ss -tlnp 2>/dev/null | grep -q ":${port} " || \
       netstat -tlnp 2>/dev/null | grep -q ":${port} "; then
        echo "WARNING: Port $port ($name) is already in use"
        return 1
    fi
    return 0
}

# Wait for a service to become available
wait_for_service() {
    local url=$1
    local name=$2
    local max_wait=$3
    local elapsed=0

    echo -n "  Waiting for $name..."
    while [ $elapsed -lt $max_wait ]; do
        if curl -s "$url" > /dev/null 2>&1; then
            echo " ready! (${elapsed}s)"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "."
    done
    echo " TIMEOUT after ${max_wait}s"
    return 1
}

# 1. Start KoboldCpp (port 5001)
echo ""
echo "[1/4] Starting KoboldCpp..."
if check_port 5001 "KoboldCpp"; then
    cd "$HOME/koboldcpp"
    nohup python3 koboldcpp.py \
        Mistral-Nemo-Instruct-2407-Q4_K_M.gguf \
        --usecublas --gpulayers 40 \
        > "$LOG_DIR/koboldcpp.log" 2>&1 &
    echo $! > "$PID_DIR/koboldcpp.pid"
    echo "  PID: $(cat "$PID_DIR/koboldcpp.pid")"

    # KoboldCpp takes a while to load the model
    wait_for_service "http://localhost:5001/api/v1/model" "KoboldCpp" 120
else
    echo "  Skipping - already running"
fi

# 2. Start Augustus Backend (port 8080)
echo ""
echo "[2/4] Starting Augustus Backend..."
if check_port 8080 "Augustus"; then
    cd "$BRIDGE_DIR/Augustus/backend"
    nohup bash -c "source $BRIDGE_DIR/Augustus/backend/augustus_venv/bin/activate && python3 -m augustus.main" \
        > "$LOG_DIR/augustus.log" 2>&1 &
    echo $! > "$PID_DIR/augustus.pid"
    echo "  PID: $(cat "$PID_DIR/augustus.pid")"

    wait_for_service "http://localhost:8080/api/agents" "Augustus" 30
else
    echo "  Skipping - already running"
fi

# 3. Start mcp-memory-service (port 8000)
echo ""
echo "[3/4] Starting mcp-memory-service..."
if check_port 8000 "mcp-memory-service"; then
    cd "$BRIDGE_DIR/mcp-memory-service"
    MCP_ALLOW_ANONYMOUS_ACCESS=true nohup python3 run_server.py \
        > "$LOG_DIR/mcp_memory.log" 2>&1 &
    echo $! > "$PID_DIR/mcp_memory.pid"
    echo "  PID: $(cat "$PID_DIR/mcp_memory.pid")"

    wait_for_service "http://localhost:8000/api/memories" "mcp-memory-service" 30
else
    echo "  Skipping - already running"
fi

# 4. Start Aeynis Chat Backend (port 5555)
echo ""
echo "[4/4] Starting Aeynis Chat Backend..."
if check_port 5555 "Aeynis Chat Backend"; then
    cd "$BRIDGE_DIR"
    nohup python3 aeynis_chat_backend.py \
        > "$LOG_DIR/aeynis_backend.log" 2>&1 &
    echo $! > "$PID_DIR/aeynis_backend.pid"
    echo "  PID: $(cat "$PID_DIR/aeynis_backend.pid")"

    wait_for_service "http://localhost:5555/api/health" "Aeynis Chat Backend" 15
else
    echo "  Skipping - already running"
fi

# Summary
echo ""
echo "========================================"
echo "  Bridge Status"
echo "========================================"
echo ""

services_ok=0
services_total=4

for pair in "5001:KoboldCpp" "8080:Augustus" "8000:mcp-memory-service" "5555:Aeynis-Backend"; do
    port="${pair%%:*}"
    name="${pair##*:}"
    if curl -s "http://localhost:$port" > /dev/null 2>&1; then
        echo "  [OK]   $name (port $port)"
        services_ok=$((services_ok + 1))
    else
        echo "  [FAIL] $name (port $port)"
    fi
done

echo ""
echo "  $services_ok/$services_total services running"
echo ""
echo "  Logs: $LOG_DIR/"
echo "  PIDs: $PID_DIR/"
echo ""

if [ $services_ok -eq $services_total ]; then
    echo "  Bridge is fully operational."
    echo "  Open aeynis_chat_simple_fixed.html to begin."
else
    echo "  WARNING: Some services failed to start."
    echo "  Check logs for details."
fi

echo "========================================"
