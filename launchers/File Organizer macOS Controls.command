#!/bin/bash
# Install or refresh the native menu bar helper and Finder Quick Action.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

exec python3 "$PROJECT_ROOT/scripts/install_macos_integrations.py" --all --launch
