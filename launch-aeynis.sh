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
MCP_CONFIG="${BRIDGE_DIR}/mcp-config.json"

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
if python3 "${KOBOLD_DIR}/koboldcpp.py" --help 2>&1 | grep -qi "mcpfile"; then
    echo "  MCP support: FOUND (--mcpfile)"
else
    echo "  WARNING: --mcpfile not found in KoboldCpp help."
    echo "  Your KoboldCpp version may not support MCP."
    echo "  Check: python3 ${KOBOLD_DIR}/koboldcpp.py --version"
    echo ""
    read -p "Continue anyway? (y/N) " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        exit 1
    fi
fi

# Generate MCP config with correct paths for this machine
cat > "${MCP_CONFIG}" << MCPEOF
{
  "mcpServers": {
    "aeynis-bridge": {
      "command": "${MCP_PYTHON}",
      "args": ["${BRIDGE_DIR}/bridge-server.py"]
    }
  }
}
MCPEOF
echo "  MCP config: ${MCP_CONFIG}"

echo ""
echo "Starting KoboldCpp with MCP Bridge Server..."
echo "  Model: ${MODEL}"
echo "  MCP config: ${MCP_CONFIG}"
echo ""

cd "${KOBOLD_DIR}"

python3 koboldcpp.py \
    "${MODEL}" \
    --usecublas --gpulayers 40 \
    --mcpfile "${MCP_CONFIG}" \
    --jinja_tools
