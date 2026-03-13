#!/bin/bash
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
echo " Organize Folder by File Type (Quick Launch)"
echo "=============================================="
echo

if [[ ! -f "$HELPER" ]]; then
  echo "Error: helper script not found:"
  echo "  $HELPER"
  echo
  read -r -p "Press Enter to close..." _
  exit 1
fi

read -r -p "Enter folder path (you can drag folder here): " TARGET_RAW
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

echo "Choose mode:"
echo "  1) Non-recursive (root files only)"
echo "  2) Recursive in-place (recommended)"
echo "  3) Recursive flatten-to-root (advanced)"
read -r -p "Mode [1/2/3] (default 2): " MODE
MODE="${MODE:-2}"

RECURSIVE_FLAG=""
STRATEGY="in-place"
case "$MODE" in
  1)
    RECURSIVE_FLAG=""
    STRATEGY="in-place"
    ;;
  2)
    RECURSIVE_FLAG="--recursive"
    STRATEGY="in-place"
    ;;
  3)
    RECURSIVE_FLAG="--recursive"
    STRATEGY="flatten-root"
    ;;
  *)
    echo "Invalid mode. Exiting."
    read -r -p "Press Enter to close..." _
    exit 1
    ;;
esac

if [[ -n "$RECURSIVE_FLAG" ]]; then
  read -r -p "Use standard normalization (JPEG->JPG + uppercase buckets)? [Y/n]: " NORM_IN
  if [[ -z "${NORM_IN:-}" ]] || is_yes "$NORM_IN"; then
    NORMALIZE="standard"
  else
    NORMALIZE="none"
  fi
else
  read -r -p "Use normalization? [y/N]: " NORM_IN
  if is_yes "$NORM_IN"; then
    NORMALIZE="standard"
  else
    NORMALIZE="none"
  fi
fi

read -r -p "Include hidden files (e.g. .DS_Store)? [y/N]: " HIDDEN_IN
if is_yes "$HIDDEN_IN"; then
  INCLUDE_HIDDEN_FLAG="--include-hidden"
else
  INCLUDE_HIDDEN_FLAG=""
fi

read -r -p "Move collectable empty folders into 'For Deletion'? [y/N]: " EMPTY_DIRS_IN
if is_yes "$EMPTY_DIRS_IN"; then
  EMPTY_DIRS_FLAG="--collect-empty-dirs"
else
  EMPTY_DIRS_FLAG=""
fi

CMD=(python3 "$HELPER" --path "$TARGET" --strategy "$STRATEGY" --normalize "$NORMALIZE")
if [[ -n "$RECURSIVE_FLAG" ]]; then
  CMD+=(--recursive)
fi
if [[ -n "$INCLUDE_HIDDEN_FLAG" ]]; then
  CMD+=(--include-hidden)
fi
if [[ -n "$EMPTY_DIRS_FLAG" ]]; then
  CMD+=(--collect-empty-dirs)
fi

echo
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
