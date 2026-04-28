#!/bin/bash
# Prompt for a folder, move every file (recursive) into top-level extension buckets,
# then delete empty folders. Safe moves only — collisions become name_1.ext, name_2.ext, …
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HELPER="$PROJECT_ROOT/scripts/organize_by_filetype.py"

is_yes() {
  local v="${1:-}"
  [[ "$v" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]
}

clean_path() {
  local p="$1"

  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"

  if [[ "${p:0:1}" == '"' && "${p: -1}" == '"' ]]; then
    p="${p:1:${#p}-2}"
  elif [[ "${p:0:1}" == "'" && "${p: -1}" == "'" ]]; then
    p="${p:1:${#p}-2}"
  fi

  p="${p//\\ / }"
  p="${p//\\~/~}"
  p="${p/#\~/$HOME}"

  if [[ "$p" != "/" ]]; then
    p="${p%/}"
  fi

  printf '%s' "$p"
}

clear

echo "=============================================="
echo " Organize Folder by File Type"
echo "=============================================="
echo
echo "This gathers every file (including inside subfolders) into"
echo "folders at the top of your chosen directory named by type"
echo "(PDF, JPG, …). Then it deletes leftover empty folders."
echo "Nothing is overwritten — duplicates get _1, _2, … before the extension."
echo

if [[ ! -f "$HELPER" ]]; then
  echo "Error: helper script not found:"
  echo "  $HELPER"
  echo
  read -r -p "Press Enter to close..." _
  exit 1
fi

read -r -p "Folder path (you can drag a folder here): " TARGET_RAW
TARGET="$(clean_path "$TARGET_RAW")"

if [[ ! -d "$TARGET" && "$TARGET" == *\\* ]]; then
  TARGET_ALT="${TARGET//\\/}"
  if [[ -d "$TARGET_ALT" ]]; then
    TARGET="$TARGET_ALT"
  fi
fi

echo
if [[ -z "$TARGET" ]]; then
  echo "No path entered. Exiting."
  read -r -p "Press Enter to close..." _
  exit 1
fi

if [[ ! -d "$TARGET" ]]; then
  echo "Folder not found: $TARGET"
  read -r -p "Press Enter to close..." _
  exit 1
fi

CMD=(
  python3 "$HELPER"
  --path "$TARGET"
  --recursive
  --strategy flatten-root
  --normalize standard
  --no-collect-empty-dirs
)

echo "----- Dry run preview -----"
"${CMD[@]}" --dry-run

echo
read -r -p "Proceed with actual run? [y/N]: " GO
if is_yes "$GO"; then
  echo
  echo "----- Running -----"
  "${CMD[@]}"
  echo
  echo "Done."
else
  echo
  echo "Cancelled (no changes made)."
fi

echo
read -r -p "Press Enter to close..." _
