# worktracker


https://github.com/user-attachments/assets/7699d502-d039-49de-9d4e-42afd98a297b


A small macOS activity tracker that helps you stop working on time.

Every ~10 seconds it records which app is in front, the current website (for Chromium browsers and Safari), and whether you are active or idle. Everything stays on your Mac: a local SQLite file and a dashboard on http://127.0.0.1:8765.

- **Dashboard**: daily and weekly totals, after-hours time, a timeline, an hour-of-day heatmap, top apps and websites with their icons.
- **Goals and alerts**: daily and weekly targets and limits, a break reminder, a wind-down prompt before the end of your workday, and nudges when you work after hours.
- **To-dos**: a Today list and a Tomorrow list in the dashboard's side panel. Tick items off, double-click to edit, move them between days, and paste several lines to add several items. Unfinished items carry over until they're done. **Close the day** marks the rest of the day as after hours, and the wind-down prompt can add tomorrow's items (separated by `;`).
- **Ask Claude** (optional): a side panel that runs your local `claude` CLI in this folder, with `CLAUDE.md` as its instructions, to answer questions about your time and notes. It can read your data and manage your to-dos or close and reopen the day, and nothing else: it runs in Claude Code's restricted mode, and its shell access is limited to `tools/query.py` (read-only SQL) and `tools/todo.py`. Drag its left edge to resize, or use ↔ to widen it.
- **Meetings**: Zoom meetings are recorded and shown on the timeline. During a meeting, alerts are held and released one at a time afterwards.
- **Personal time**: a toolbar button pauses tracking and alerts for 30 or 60 minutes (configurable) and alerts you when it ends.
- **Private browsing**: sites in Chrome, Brave, Edge, Chromium and Vivaldi incognito windows are not recorded (the time still counts under the browser). Safari and Arc private windows can't be detected and are recorded normally.
- **Idle handling**: short pauses count as work; once you have been idle past the threshold (5 min by default), or lock the screen, that idle tail is removed from your work time. While a call app is in front (`idle_exempt_apps`: Zoom, FaceTime, Teams by default) you count as active without input, so don't leave one in front when you step away.

How it works inside, and the design decisions behind it, are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Install

### One-prompt setup with an AI agent

Paste this into Claude Code, Codex or any coding agent that can run commands on your Mac:

```text
Install worktracker (https://github.com/yotammartin/worktracker) on this Mac.
1. Check this is macOS and that python3 is 3.10 or newer. If it isn't, ask me before installing Python with Homebrew.
2. Clone the repo to ~/Code/worktracker (or `git pull` if it's already there) and read README.md.
3. Run ./install.sh from that folder and wait for it to print the dashboard address.
4. Confirm http://127.0.0.1:8765 responds; if not, show me the end of data/logs/worktracker.log and data/logs/launchd.err.
5. Tell me which macOS prompts to accept (browser Automation, and Script Editor under Notifications), and how to uninstall.
Don't change any code or settings beyond this.
```

### Manual

**Requirements**
- macOS. Developed and tested on macOS 26 on Apple Silicon; nothing in the code is specific to Apple Silicon.
- Python 3.10+ (`brew install python`).
- For website tracking: Chrome, Brave, Edge, Chromium, Vivaldi, Arc or Safari.
- Optional, for **Ask Claude**: [Claude Code](https://code.claude.com) installed as `claude` in `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`, and logged in once in a terminal. Tracking works without it.

```bash
git clone https://github.com/yotammartin/worktracker.git ~/Code/worktracker
cd ~/Code/worktracker
./install.sh
```

This creates `.venv/` (dependencies from `requirements.txt`), stores data in `data/`, and registers a launch agent that starts at login and restarts if it crashes. The installer waits until the dashboard answers and prints its address. Keep the folder where it is: the launch agent runs the code from there.

**First run**
- When macOS asks to let the tracker control your browser, click **Allow**, or the website breakdown stays empty. To change it later: System Settings → Privacy & Security → Automation.
- Banner alerts are sent through AppleScript, so they appear under **Script Editor** in System Settings → Notifications. Turn that entry on to see them.

**Afterwards**
- **App in the Dock**: open the dashboard in Chrome and choose the install icon at the right of the address bar (or ⋮ → Cast, save and share → Install page as app), then right-click its Dock icon → Options → Keep in Dock. In Safari on macOS 14+: File → Add to Dock.
- **Update**: `git pull`, then `./install.sh`. Dependencies are reinstalled when `requirements.txt` changes. Your data is kept.
- **Remove**: `./install.sh --uninstall` stops the agent and removes the launch agent. `data/` and `.venv/` stay; delete the folder to remove everything.
- **Back up**: copy `data/` while the agent is stopped (`./install.sh --uninstall`, copy, `./install.sh`). Copying `tracker.db` alone while it runs can miss recent changes, which live in `tracker.db-wal` until checkpointed.
- **Try it with sample data**: the **Data** switch at the bottom of the dashboard flips between **Your data** and **Sample data**. The first switch generates three randomised weeks in `data-mock/`, and to regenerate them, delete that folder. The dashboard, to-dos and Ask Claude (with its own chat history) all follow the switch, while your activity keeps being recorded into your own data. It resets to your data when the agent restarts.

## Configure

Use **Goals & settings** on the dashboard, or edit `data/config.json` (changes apply within seconds, except `sample_seconds` and `dashboard_port`, which need `./install.sh`). An invalid file is rejected as a whole: the tracker keeps its previous settings and logs the reason in `data/logs/worktracker.log`. Notable keys: `work_days` (0 = Monday … 6 = Sunday), `week_starts_on`, `day_starts_at_hour`, `work_start`/`work_end`, `goals`, and `alerts` (including `app_limits_minutes`, `domain_limits_minutes`, `hold_during_zoom_meetings`, and `early_start_from`: on work days, no after-hours alerts from this time until `work_start`).

Alerts come in three styles, chosen separately for routine and important alerts (`alerts.routine_style` / `alerts.important_style`, or in settings, which also has preview buttons):

- `banner`: a macOS notification. Sent through AppleScript, so it is listed under **Script Editor** in System Settings → Notifications, and Focus modes can hide it.
- `dialog`: a system dialog with Snooze / OK that closes itself after 2 minutes.
- `overlay`: a floating card at the top right, above all windows and spaces, that stays until you close it.

The end-of-day prompt always uses a dialog because it takes text input (or a banner, if important alerts are set to `banner`).

## Privacy

`data/` holds your activity history and is git-ignored. The dashboard listens on 127.0.0.1 only, refuses to be embedded in other pages, and rejects requests from other websites. Website icons are read from your browser's local icon cache; nothing is sent to external services. The exception is **Ask Claude**: your questions and what Claude reads to answer them, such as query results and notes, are sent to Anthropic under your Claude account.

The local API has no login. Any program running on your Mac, including under another user account, can read your stats through it. Use worktracker on a Mac you don't share.

## Known limitations

- Zoom meetings are detected from Zoom's in-meeting helper process (`CptHost`), which Zoom doesn't document. Google Meet and Teams count as work only while they're in front, and alerts aren't held for them.
- Safari and Arc private windows can't be told apart from normal ones, so they're recorded.
- `after_hours` is decided when time is recorded. Changing your work hours later doesn't relabel history, and the timeline colours past days using your current hours.
- History is stored in local time. After changing time zone, older days keep their original hours.
- Reading the browser's URL can briefly delay sampling while a browser is busy or a permission prompt is open.

## License

[MIT](LICENSE). The Claude Code mascot ("Clawd") in the chat button is Anthropic's artwork and isn't covered by this license; see [Anthropic's trademark guidelines](https://www.anthropic.com/legal/trademark-guidelines).
