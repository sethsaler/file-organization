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
if [[ -d "$SRC/macos" ]]; then
  cp -R "$SRC/macos" "$INSTALL_DIR/"
fi
if [[ -d "$SRC/install" ]]; then
  cp -R "$SRC/install" "$INSTALL_DIR/"
fi
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

BIN_DIR="${FILE_ORG_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
WRAPPER="$BIN_DIR/organize-by-filetype"
cat > "$WRAPPER" << WRAP
#!/usr/bin/env sh
exec python3 "$INSTALL_DIR/scripts/organize_by_filetype.py" "\$@"
WRAP
chmod +x "$WRAPPER"
RESTORE_WRAPPER="$BIN_DIR/restore-file-organization"
cat > "$RESTORE_WRAPPER" << WRAP
#!/usr/bin/env sh
exec python3 "$INSTALL_DIR/scripts/restore_from_backup.py" "\$@"
WRAP
chmod +x "$RESTORE_WRAPPER"
CONTROL_WRAPPER="$BIN_DIR/file-organizer-control"
cat > "$CONTROL_WRAPPER" << WRAP
#!/usr/bin/env sh
exec python3 "$INSTALL_DIR/scripts/quick_controls.py" "\$@"
WRAP
chmod +x "$CONTROL_WRAPPER"

# Best-effort: install the optional `watchdog` package so watch mode uses native
# filesystem events (near-instant) instead of the mtime-polling fallback.
if ! python3 -c "import watchdog" >/dev/null 2>&1; then
  echo "Installing optional dependency: watchdog (native FS events for watch mode)…"
  python3 -m pip install --user --quiet "watchdog>=3.0" >/dev/null 2>&1 \
    || python3 -m pip install --user --quiet --break-system-packages "watchdog>=3.0" >/dev/null 2>&1 \
    || echo "  Skipped (pip install failed) — watch mode will use polling fallback."
fi

echo "Done."
echo
echo "CLI on PATH (if ~/.local/bin is in PATH):"
echo "  organize-by-filetype --path /path/to/folder"
echo "  restore-file-organization MANIFEST.json"
echo "  file-organizer-control status"
echo "Command Center (organize, rules, review, safety, schedule, history):"
echo "  python3 \"$INSTALL_DIR/scripts/command_center.py\""
echo "Schedule-only window (same Schedule tab):"
echo "  python3 \"$INSTALL_DIR/scripts/schedule_gui.py\""
echo "Background scheduler (systemd / cron / launchd; see $INSTALL_DIR/install/):"
echo "  python3 \"$INSTALL_DIR/scripts/schedule_daemon.py\" --foreground"
echo "  python3 \"$INSTALL_DIR/scripts/schedule_daemon.py\" --once"
echo
echo "macOS double-click (after copying launchers to Desktop or opening in Finder):"
echo "  $INSTALL_DIR/launchers/Organize by File Type (Tinker).command"
echo "  $INSTALL_DIR/launchers/File Organizer macOS Controls.command"
echo "  $INSTALL_DIR/launchers/Organize Files by Type.command"
echo "  $INSTALL_DIR/launchers/Organize Desktop by File Type.command"
