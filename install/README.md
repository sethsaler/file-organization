# Background scheduling (optional)

- **Linux (systemd):** see [systemd/README.md](systemd/README.md) for a user service or cron.
- **macOS (LaunchAgent):** copy and edit [launchd/org.fileorganization.schedule-daemon.plist.example](launchd/org.fileorganization.schedule-daemon.plist.example) — replace `INSTALL_DIR` with your install path, then:

```bash
mkdir -p ~/Library/LaunchAgents INSTALL_DIR/logs
cp org.fileorganization.schedule-daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/org.fileorganization.schedule-daemon.plist
```

The daemon runs `schedule_daemon.py --foreground`; schedule folders with `schedule_gui.py` and set `scheduler_enabled` to true in `schedule.json` (or pass `--force` on the daemon).
