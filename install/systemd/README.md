# systemd — background organizer

Install (`sudo`) unit files under `/etc/systemd/user/` (user session) or `/etc/systemd/system/` (system).

Replace placeholders:

- `INSTALL_DIR` — project directory containing `scripts/` (for example `~/.local/share/organize-folder-by-filetype`)

## Option A: user service (runs while you are logged in)

Create `~/.config/systemd/user/file-org-scheduler.service`:

```ini
[Unit]
Description=File organization scheduler (schedule_daemon.py)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/.local/share/organize-folder-by-filetype/scripts
ExecStart=/usr/bin/python3 %h/.local/share/organize-folder-by-filetype/scripts/schedule_daemon.py --foreground
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now file-org-scheduler.service
journalctl --user -u file-org-scheduler.service -f
```

Set `scheduler_enabled` to true in `schedule.json` (via the GUI), or run the daemon with `--force` to ignore that flag.

## Option B: cron + `--once`

Add a crontab line. **Once per day at midnight** (set `"schedule_mode": "daily"` and `"daily_time": "00:00"` in `schedule.json`, or use interval mode with a long `interval_minutes`):

```cron
0 0 * * * /usr/bin/python3 /FULL/PATH/TO/scripts/schedule_daemon.py --once >>"$HOME/.cache/file-org-scheduler.log" 2>&1
```

Every hour at minute 0:

```cron
0 * * * * /usr/bin/python3 /FULL/PATH/TO/scripts/schedule_daemon.py --once >>"$HOME/.cache/file-org-scheduler.log" 2>&1
```

Ensure `scheduler_enabled` is true in `schedule.json`, or use `--force`.
