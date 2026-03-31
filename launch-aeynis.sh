#!/bin/bash
#
# launch-aeynis.sh - Start KoboldCpp with MCP Bridge Server
#
# Launches KoboldCpp with Mistral-Nemo and connects the Aeynis
# Bridge MCP Server via STDIO transport.
#
# Usage: ~/bridge/launch-aeynis.sh
#

set -e

BRIDGE_DIR="$HOME/bridge"
KOBOLD_DIR="$HOME/koboldcpp"
MODEL="Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"
MCP_VENV="${BRIDGE_DIR}/mcp-venv"
MCP_PYTHON="${MCP_VENV}/bin/python3"
MCP_SERVER="${MCP_PYTHON} ${BRIDGE_DIR}/bridge-server.py"

echo "========================================"
echo "  Launching Aeynis with MCP Bridge"
echo "  $(date)"
echo "========================================"

# Verify bridge server exists
if [ ! -f "${BRIDGE_DIR}/bridge-server.py" ]; then
    echo "ERROR: bridge-server.py not found at ${BRIDGE_DIR}"
    exit 1
fi

# Verify MCP venv and SDK
if [ ! -f "${MCP_PYTHON}" ]; then
    echo "Creating MCP virtual environment..."
    python3 -m venv "${MCP_VENV}"
fi
if ! "${MCP_PYTHON}" -c "import mcp" 2>/dev/null; then
    echo "Installing MCP SDK in venv..."
    "${MCP_VENV}/bin/pip" install "mcp[cli]"
fi

# Verify KoboldCpp exists
if [ ! -f "${KOBOLD_DIR}/koboldcpp.py" ]; then
    echo "ERROR: KoboldCpp not found at ${KOBOLD_DIR}"
    exit 1
fi

# Check KoboldCpp MCP support
echo ""
echo "Checking KoboldCpp MCP support..."
if python3 "${KOBOLD_DIR}/koboldcpp.py" --help 2>&1 | grep -qi "mcp"; then
    echo "  MCP support: FOUND"
    MCP_FLAG="--mcp_server"
else
    echo "  WARNING: MCP flag not found in KoboldCpp help."
    echo "  Your KoboldCpp version may not support MCP."
    echo "  Check: python3 ${KOBOLD_DIR}/koboldcpp.py --version"
    echo ""
    echo "  If MCP is not supported, the bridge server can still run"
    echo "  standalone for testing with MCP Inspector."
    echo ""
    read -p "Continue anyway? (y/N) " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        exit 1
    fi
    MCP_FLAG=""
fi

echo ""
echo "Starting KoboldCpp with MCP Bridge Server..."
echo "  Model: ${MODEL}"
echo "  MCP Server: ${MCP_SERVER}"
echo ""

cd "${KOBOLD_DIR}"

if [ -n "$MCP_FLAG" ]; then
    # Launch with MCP STDIO transport
    python3 koboldcpp.py \
        "${MODEL}" \
        --usecublas --gpulayers 40 \
        ${MCP_FLAG} "stdio:${MCP_SERVER}"
else
    # Launch without MCP (user will need to test separately)
    echo "Launching KoboldCpp without MCP (no --mcp_server support detected)"
    python3 koboldcpp.py \
        "${MODEL}" \
        --usecublas --gpulayers 40
fi
