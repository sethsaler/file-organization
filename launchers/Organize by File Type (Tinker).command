#!/bin/bash
# Double-click to open the Tk “tinker” GUI (pick folder, options, dry-run / run).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GUI="$PROJECT_ROOT/scripts/tinker_gui.py"

clear

echo "=============================================="
echo " Organize by File Type — Tinker GUI"
echo "=============================================="
echo

if [[ ! -f "$GUI" ]]; then
  echo "Error: GUI script not found:"
  echo "  $GUI"
  echo
  read -r -p "Press Enter to close..." _
  exit 1
fi

exec python3 "$GUI"
