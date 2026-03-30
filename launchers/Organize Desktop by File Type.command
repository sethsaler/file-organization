#!/bin/bash
# One-click: organize all files on your Desktop into extension folders (JPG, PDF, …).
# Safe moves only — never overwrites; duplicate names become name_1.ext, name_2.ext, …
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$PROJECT_ROOT/scripts/organize_by_filetype.py"

DESKTOP="${HOME}/Desktop"

clear

echo "=============================================="
echo " Organize Desktop by File Type (one-click)"
echo "=============================================="
echo
echo "Target: $DESKTOP"
echo

if [[ ! -f "$HELPER" ]]; then
  echo "Error: helper script not found:"
  echo "  $HELPER"
  echo
  read -r -p "Press Enter to close..." _
  exit 1
fi

if [[ ! -d "$DESKTOP" ]]; then
  echo "Error: Desktop folder not found:"
  echo "  $DESKTOP"
  echo
  read -r -p "Press Enter to close..." _
  exit 1
fi

# Recursive in-place: each folder on Desktop keeps its own type subfolders.
# Standard normalization: JPEG/JPE -> JPG, uppercase buckets.
# No empty-folder staging: only moves files into type folders (no folder quarantine).
JSON="$(python3 "$HELPER" \
  --path "$DESKTOP" \
  --recursive \
  --strategy in-place \
  --normalize standard \
  --no-collect-empty-dirs)"

echo "$JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if 'error' in data:
    print('Error:', data['error'])
    sys.exit(1)
print('Files moved:', data.get('files_moved', 0))
coll = data.get('name_collisions_resolved', 0)
if coll:
    print('Duplicate names renamed (suffix _1, _2, …):', coll)
print('Folders touched:', data.get('folders_touched', 0))
print()
print('Full JSON summary follows.')
print()
print(json.dumps(data, indent=2))
"

echo
read -r -p "Press Enter to close..." _
