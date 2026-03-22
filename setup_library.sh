#!/bin/bash
#
# setup_library.sh - Pull and set up the Aeynis Library locally
#
# Run from your bridge directory: ~/bridge/setup_library.sh
#
# What this does:
#   1. Stops Aeynis services (gracefully)
#   2. Pulls the library code from GitHub
#   3. Installs dependencies (poppler-utils, PyPDF2, beautifulsoup4)
#   4. Creates the ~/AeynisLibrary directory structure
#   5. Restarts Aeynis services
#

set -e

BRIDGE_DIR="$HOME/bridge"
BRANCH="claude/setup-aeynis-bridge-rJLxu"

echo "========================================"
echo "  Aeynis Library Setup"
echo "  $(date)"
echo "========================================"
echo ""

# Step 1: Stop services
echo "[1/5] Stopping Aeynis services..."
if [ -f "$BRIDGE_DIR/stop_aeynis.sh" ]; then
    bash "$BRIDGE_DIR/stop_aeynis.sh" 2>/dev/null || true
    echo "  Services stopped."
else
    echo "  stop_aeynis.sh not found, skipping."
fi
echo ""

# Step 2: Pull latest code
echo "[2/5] Pulling latest code from branch: $BRANCH"
cd "$BRIDGE_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH" "origin/$BRANCH"
git pull origin "$BRANCH"
echo "  Code updated."
echo ""

# Step 3: System dependencies
echo "[3/5] Installing system dependencies..."
echo "  This may ask for your sudo password."
sudo apt install -y poppler-utils 2>/dev/null && echo "  poppler-utils installed." || echo "  WARNING: Could not install poppler-utils. PDF extraction will use Python fallback."
echo ""

# Step 4: Python dependencies
echo "[4/5] Installing Python dependencies..."
pip install --user PyPDF2 beautifulsoup4 2>/dev/null && echo "  PyPDF2 + beautifulsoup4 installed." || pip3 install --user PyPDF2 beautifulsoup4 2>/dev/null && echo "  PyPDF2 + beautifulsoup4 installed." || echo "  WARNING: Could not install Python packages. Some features may be limited."
echo ""

# Step 5: Create library directory and restart
echo "[5/5] Creating library directory and restarting services..."
mkdir -p "$HOME/AeynisLibrary/originals" "$HOME/AeynisLibrary/reviews" "$HOME/AeynisLibrary/imports"
echo "  Created ~/AeynisLibrary/"
echo "    originals/  - Aeynis's own documents"
echo "    reviews/    - Her reviews and annotations"
echo "    imports/    - Files you give her to read"
echo ""

echo "  Starting Aeynis..."
bash "$BRIDGE_DIR/start_aeynis.sh"

echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Library: ~/AeynisLibrary/ (50GB limit)"
echo "  Open the chat and click the Library"
echo "  button to browse files."
echo "========================================"
