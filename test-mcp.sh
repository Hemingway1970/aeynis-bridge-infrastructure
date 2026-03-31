#!/bin/bash
#
# test-mcp.sh - Test Aeynis Bridge MCP Server
#
# Phase 1: Verify infrastructure
# Phase 2: Test minimal server with MCP Inspector
# Phase 3: Test full server
#
# Usage: ~/bridge/test-mcp.sh
#

BRIDGE_DIR="$HOME/bridge"
KOBOLD_DIR="$HOME/koboldcpp"
MCP_VENV="${BRIDGE_DIR}/mcp-venv"
MCP_PYTHON="${MCP_VENV}/bin/python3"

echo "========================================"
echo "  Aeynis MCP Bridge - Test Suite"
echo "  $(date)"
echo "========================================"

# ── Phase 1: Verify Infrastructure ─────────────────────────

echo ""
echo "=== Phase 1: Verify Infrastructure ==="
echo ""

# Check Python
echo -n "  Python 3: "
python3 --version 2>&1 || echo "NOT FOUND"

# Check MCP venv and SDK
echo -n "  MCP venv: "
if [ -f "${MCP_PYTHON}" ]; then
    echo "OK"
else
    echo "NOT FOUND — run: python3 -m venv ${MCP_VENV}"
fi
echo -n "  MCP SDK: "
if "${MCP_PYTHON}" -c "import mcp; print(f'v{mcp.__version__}')" 2>/dev/null; then
    :
else
    echo "NOT INSTALLED"
    echo "  Install with: ${MCP_VENV}/bin/pip install 'mcp[cli]'"
fi

# Check KoboldCpp version
echo -n "  KoboldCpp: "
if [ -f "${KOBOLD_DIR}/koboldcpp.py" ]; then
    python3 "${KOBOLD_DIR}/koboldcpp.py" --version 2>&1 | head -1 || echo "found but version check failed"
else
    echo "NOT FOUND at ${KOBOLD_DIR}"
fi

# Check KoboldCpp MCP support
echo -n "  MCP support: "
if python3 "${KOBOLD_DIR}/koboldcpp.py" --help 2>&1 | grep -qi "mcp"; then
    echo "AVAILABLE"
else
    echo "NOT DETECTED (may need KoboldCpp v1.106+)"
fi

# Check bridge server
echo -n "  Bridge server: "
if [ -f "${BRIDGE_DIR}/bridge-server.py" ]; then
    echo "FOUND"
else
    echo "NOT FOUND"
fi

# Check writings directory
echo -n "  Writings dir: "
if [ -d "$HOME/AeynisLibrary/writings" ]; then
    count=$(ls -1 "$HOME/AeynisLibrary/writings" 2>/dev/null | wc -l)
    echo "OK ($count files)"
else
    echo "NOT FOUND"
fi

# Check calendar
echo -n "  Calendar: "
if [ -f "$HOME/AeynisLibrary/calendar/events.json" ]; then
    count=$(python3 -c "import json; print(len(json.load(open('$HOME/AeynisLibrary/calendar/events.json'))))" 2>/dev/null)
    echo "OK ($count events)"
else
    echo "NO EVENTS FILE"
fi

# Check Node.js for MCP Inspector
echo -n "  Node.js (for MCP Inspector): "
if command -v node &>/dev/null; then
    node --version
else
    echo "NOT FOUND (optional - install for MCP Inspector)"
fi

# ── Phase 2: Test Minimal Server ───────────────────────────

echo ""
echo "=== Phase 2: Test Minimal Server ==="
echo ""

# Quick syntax check
echo -n "  Syntax check: "
if "${MCP_PYTHON}" -c "import py_compile; py_compile.compile('${BRIDGE_DIR}/bridge-server.py', doraise=True)" 2>/dev/null; then
    echo "PASS"
else
    echo "FAIL"
fi

# Test that server can initialize (send init request, check response)
echo -n "  Server init test: "
INIT_REQUEST='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
RESPONSE=$(echo "$INIT_REQUEST" | timeout 5 "${MCP_PYTHON}" "${BRIDGE_DIR}/bridge-server.py" --test 2>/dev/null | head -1)
if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'result' in d; print('PASS')" 2>/dev/null; then
    :
else
    echo "FAIL (server may not have responded correctly)"
    echo "  Response: ${RESPONSE:0:200}"
fi

# ── Phase 3 Instructions ──────────────────────────────────

echo ""
echo "=== Phase 3: Full Server Test ==="
echo ""
echo "  To test the full server with MCP Inspector:"
echo "    npx @modelcontextprotocol/inspector python3 ${BRIDGE_DIR}/bridge-server.py"
echo ""
echo "  To test with KoboldCpp (if MCP supported):"
echo "    bash ${BRIDGE_DIR}/launch-aeynis.sh"
echo "    Then ask Aeynis to:"
echo "      - 'Write a reflection about bridges'"
echo "      - 'List my documents'"
echo "      - 'Add a calendar event for tomorrow: story session'"
echo "      - 'What's on my calendar?'"
echo ""
echo "  To verify state persistence:"
echo "    cat ~/.aeynis-state/state.json"
echo ""
echo "========================================"
