# worktracker

A small macOS activity tracker that helps you stop working on time.

Every ~10 seconds it records which app is in front, the current website (for Chromium browsers and Safari), and whether you are active or idle. Everything stays on your Mac: a local SQLite file and a dashboard on http://127.0.0.1:8765.

- **Dashboard**: daily and weekly totals, after-hours time, a timeline, an hour-of-day heatmap, top apps and websites with their icons.
- **Goals and alerts**: daily and weekly targets and limits, a break reminder, a wind-down prompt before the end of your workday, and nudges when you work after hours.
- **Tomorrow's first step**: the wind-down prompt asks what you will start with tomorrow; saving it closes the day and the note is shown the next morning.
- **Meetings**: during a Zoom meeting, alerts are held and released one at a time afterwards.
- **Idle handling**: short pauses count as work; once you have been idle past the threshold (5 min by default), that idle tail is removed from your work time.

## Install

Requires macOS and Python 3.10+ (`brew install python`).

```bash
./install.sh
```

This creates `.venv/`, stores data in `data/`, and registers a launch agent that starts at login and restarts if it crashes. The first time, allow macOS to let it control your browser (System Settings → Privacy & Security → Automation), or the website breakdown stays empty.

- **Update**: edit the code, then run `./install.sh` again. Your data is kept.
- **Remove**: `./install.sh --uninstall` stops the agent and keeps `data/`.

## Configure

Use **Goals & settings** on the dashboard, or edit `data/config.json` (changes apply within seconds, except `sample_seconds` and `dashboard_port`). Notable keys: `work_days` (0 = Monday … 6 = Sunday), `week_starts_on`, `day_starts_at_hour`, `work_start`/`work_end`, `goals`, and `alerts` (including `app_limits_minutes`, `domain_limits_minutes` and `hold_during_zoom_meetings`).

Banners are sent through AppleScript, so they appear under **Script Editor** in System Settings → Notifications.

## Privacy

`data/` holds your activity history and is git-ignored. The dashboard listens on 127.0.0.1 only. Website icons are read from your browser's local icon cache; nothing is sent to external services.
