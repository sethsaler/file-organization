#!/usr/bin/env bash
# Install or refresh this project from GitHub (scripts + launchers + docs).
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/sethsaler/file-organization/main/scripts/install.sh | bash
# Optional environment:
#   FILE_ORG_REF=main              branch, tag, or commit (default: main)
#   FILE_ORG_INSTALL_DIR=...      target directory (default: ~/.local/share/organize-folder-by-filetype)
#   FILE_ORG_REPO=owner/name       override GitHub repo slug (default: sethsaler/file-organization)

set -euo pipefail

REPO_SLUG="${FILE_ORG_REPO:-sethsaler/file-organization}"
REF="${FILE_ORG_REF:-main}"
INSTALL_DIR="${FILE_ORG_INSTALL_DIR:-$HOME/.local/share/organize-folder-by-filetype}"

REF_ENC="$(REF="$REF" python3 -c "import os, urllib.parse; print(urllib.parse.quote(os.environ['REF'], safe=''))")"

TMP="${TMPDIR:-/tmp}/file-org-install.$$"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP"

echo "Installing organize-folder-by-filetype"
echo "  Repository: $REPO_SLUG"
echo "  Ref:        $REF"
echo "  Into:       $INSTALL_DIR"
echo

curl -fsSL "https://codeload.github.com/${REPO_SLUG}/tar.gz/${REF_ENC}" | tar -xz -C "$TMP"

SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "$SRC" || ! -d "$SRC/scripts" ]]; then
  echo "Error: archive layout unexpected (missing scripts/). Check FILE_ORG_REF and FILE_ORG_REPO." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cp -R "$SRC/scripts" "$SRC/launchers" "$INSTALL_DIR/"
for doc in README.md LICENSE SKILL.md CHANGELOG.md; do
  if [[ -f "$SRC/$doc" ]]; then
    cp "$SRC/$doc" "$INSTALL_DIR/"
  fi
done

chmod +x "$INSTALL_DIR/scripts/install.sh" 2>/dev/null || true
shopt -s nullglob
for launcher in "$INSTALL_DIR/launchers/"*.command; do
  chmod +x "$launcher"
done
shopt -u nullglob

echo "Done."
echo
echo "CLI helper:"
echo "  python3 \"$INSTALL_DIR/scripts/organize_by_filetype.py\" --path /path/to/folder"
echo
echo "macOS double-click (after copying launchers to Desktop or opening in Finder):"
echo "  $INSTALL_DIR/launchers/Organize by File Type (Tinker).command"
echo "  $INSTALL_DIR/launchers/Organize Files by Type.command"
echo "  $INSTALL_DIR/launchers/Organize Desktop by File Type.command"
