# Background scheduling

The Schedule tab **enables a background daemon automatically** when you turn on automatic runs:

- **macOS:** installs a LaunchAgent at `~/Library/LaunchAgents/org.fileorganization.schedule-daemon.plist`
- **Linux:** installs a systemd user unit at `~/.config/systemd/user/file-org-scheduler.service`

Logs: `~/.local/state/file-organization/` (or `$XDG_STATE_HOME/file-organization/`).

Manual setup (optional) — same behavior as the GUI:

- **Linux (systemd):** see [systemd/README.md](systemd/README.md)
- **macOS (LaunchAgent):** copy and edit [launchd/org.fileorganization.schedule-daemon.plist.example](launchd/org.fileorganization.schedule-daemon.plist.example)
