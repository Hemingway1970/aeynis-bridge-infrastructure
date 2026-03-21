#!/bin/bash
#
# stop_aeynis.sh - Cleanly stop all Aeynis Bridge services
#
# Usage: ~/bridge/stop_aeynis.sh
#
# Stops services in reverse order (backend first, inference last)
#

BRIDGE_DIR="$HOME/bridge"
PID_DIR="$BRIDGE_DIR/pids"

echo "========================================"
echo "  Stopping Aeynis Bridge Infrastructure"
echo "  $(date)"
echo "========================================"

# Stop a service by PID file
stop_service() {
    local name=$1
    local pid_file="$PID_DIR/$2.pid"

    echo -n "  Stopping $name... "

    if [ -f "$pid_file" ]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            # Wait up to 10 seconds for graceful shutdown
            local waited=0
            while kill -0 "$pid" 2>/dev/null && [ $waited -lt 10 ]; do
                sleep 1
                waited=$((waited + 1))
            done

            if kill -0 "$pid" 2>/dev/null; then
                echo -n "force killing... "
                kill -9 "$pid" 2>/dev/null
                sleep 1
            fi

            if ! kill -0 "$pid" 2>/dev/null; then
                echo "stopped (PID $pid)"
            else
                echo "FAILED to stop (PID $pid)"
            fi
        else
            echo "not running (stale PID $pid)"
        fi
        rm -f "$pid_file"
    else
        echo "no PID file found"
    fi
}

echo ""

# Stop in reverse order (dependents first)
stop_service "Memory Consolidator" "consolidator"
stop_service "Aeynis Chat Backend" "aeynis_backend"
stop_service "mcp-memory-service"  "mcp_memory"
stop_service "Augustus Backend"    "augustus"
stop_service "KoboldCpp"           "koboldcpp"

echo ""
echo "========================================"
echo "  All services stopped"
echo "========================================"
