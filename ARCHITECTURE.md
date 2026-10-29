# Architecture

How worktracker is built and why. For setup and usage, see [README.md](README.md). For querying the data, see [CLAUDE.md](CLAUDE.md).

## Overview

```
                       launchd (user agent, RunAtLoad + KeepAlive)
                                      │
                                      ▼
┌──────────────────────────── tracker.py (one process) ────────────────────────────┐
│                                                                                  │
│  main thread: NSApplication (accessory, no Dock icon)                            │
│    NSTimer every sample_seconds ──► Tracker.tick()                               │
│        sample(): front app, browser URL, HID idle, screen lock, Zoom meeting     │
│        write usage / sessions / meetings ──► SQLite (data/tracker.db)            │
│        check_alerts() ──► banner | dialog | overlay (NSPanel)                    │
│                                                                                  │
│  HTTP thread: ThreadingHTTPServer on 127.0.0.1:8765                              │
│    GET  /            dashboard.html (single file, no build)                      │
│    GET  /api/stats   all dashboard data for a date range, one JSON payload       │
│    GET  /api/icon    app icon (macOS bundle) or favicon (browser icon cache)     │
│    POST /api/{config,close,todo,personal,test-alert,dataset}                             │
│    POST /api/chat    ──► spawns `claude -p` and streams NDJSON back              │
└──────────────────────────────────────────────────────────────────────────────────┘
```

There are no dependencies beyond PyObjC: the standard library's `http.server`, `sqlite3` and `subprocess`, plus AppleScript through `NSAppleScript` and `osascript`. The dashboard is plain HTML, CSS and JavaScript with inline SVG charts. Nothing needs to be built.

## Files

| Path | Role |
|---|---|
| `tracker.py` | Sampler, alert engine, HTTP API and chat bridge. One process, two threads. |
| `dashboard.html` | The whole UI: timeline, KPIs, charts, tables, notes, settings dialog, chat panel. |
| `static/` | App icons and the web-app manifest, so Chrome or Safari can install the dashboard as an app. |
| `install.sh` | Builds `.venv`, writes the launchd plist and (re)starts the agent. Run it again after code changes. |
| `tools/query.py` | Runs one read-only SQL query and prints a table; `--settings` prints the effective settings. The chat's only way to read data. |
| `tools/todo.py` | Lists, adds, ticks off, edits, moves and removes to-dos, and closes or reopens the day, through the HTTP API. The only write path the chat may use. |
| `tools/common.py` | Shared by the helpers: resolves the data folder (`$WORKTRACKER_HOME`, else `data/`) and calls the local API. |
| `requirements.txt` | PyObjC version range. `install.sh` reinstalls when it changes. |
| `tools/make_mock_data.py` | Generates a seeded, randomised dataset in `data-mock/`, used by the dashboard's Data switch. |
| `data/` | `tracker.db`, `config.json` and `logs/`. Git-ignored. |

## Sampling loop

`Tracker.tick()` runs on an `NSTimer` (default 10 s, with 30 % tolerance so macOS can group wake-ups). Each tick:

1. **Reloads config** if `config.json`'s mtime changed. The merged settings are validated as a whole (`check_config`); an invalid file is rejected and the previous settings stay in effect. Week-start or day-start changes reload the day's running totals.
2. **Sleep detection:** `dt` is the time since the last tick, and it counts only if it is ≤ 3 × `sample_seconds`. Longer gaps (sleep, a stuck timer) count as nothing, so a closed lid never becomes work.
3. **Personal time:** while a block runs, nothing is sampled or written. The time counts as a break.
4. **Sampling:**
   - **Idle:** `CGEventSourceSecondsSinceLastEventType`, the seconds since the last keyboard or mouse input.
   - **Front app:** `NSWorkspace.frontmostApplication`.
   - **Website:** browser URL via a precompiled `NSAppleScript`.
   - **Lock:** `CGSessionCopyCurrentDictionary()["CGSSessionScreenIsLocked"]`.
   - **Zoom meeting:** `pgrep -x CptHost`. Zoom runs that helper only during a meeting. It isn't documented, so it's a known fragility.
5. **Classification:** a tick is `active` when the screen is unlocked and either idle is below the threshold or the tick is *exempt*. Exempt means a Zoom meeting, or an app or domain from `idle_exempt_*` in front.
6. **Writing:** `dt` seconds are upserted into `usage`, keyed by (`day`, `hour`, `app`, `domain`, `active`, `after_hours`).

### Logical day and week

`day = date(ts − day_starts_at_hour)`, so with the default 4am, 1am activity belongs to the previous day. `hour` stays the real clock hour. Weeks start on `week_starts_on`. Config weekdays use Python's Monday = 0, while SQLite's `%w` uses Sunday = 0. `after_hours` is fixed when the row is written (outside the work window, on a non-work day, or after the day was closed) and is never recomputed.

### Idle trimming

The idle threshold (5 min) means the first minutes of every break look like work. Instead of shortening the threshold, which would penalise reading and thinking, the tracker keeps a short buffer of recent, non-exempt active credits (`self.recent`). When the threshold confirms you're away, or the screen locks, or a sleep gap is detected, `trim_idle_tail(last_input)` moves every credit after the last input from `active = 1` to `active = 0`. It also cuts the session back to `last_input` and rewinds the running totals.

The result: pauses under the threshold count as work, and longer breaks add nothing. Meeting ticks clear the buffer, so a trim never reaches back into a call.

### Sessions and meetings

- **Sessions:** a `sessions` row is a continuous active stretch. It is extended while ticks are less than `break_minutes_to_reset` apart, and a new row starts after that. The timeline and focus metrics read sessions; totals read `usage`.
- **Meetings:** a `meetings` row opens when `CptHost` appears and is extended every tick until it exits, so times are accurate to about one sample. A sleep gap ends the meeting where it was last seen, and a new row opens if the meeting is still running after wake.

## Alerts

`check_alerts()` evaluates a fixed set of triggers:
- pace toward the daily and weekly targets;
- the daily and weekly limits;
- continuous work without a break;
- after-hours minutes;
- the wind-down prompt before the end of the work window;
- per-app and per-domain limits.

Each alert has a key and fires at most once per scope (a day, or `W<week-start>` for weekly alerts). `fire()` is the single gate:

- **Snooze** and **Zoom hold** return *before* the alert is recorded. That postpones it rather than dropping it, so it fires on a later tick if its condition still holds. After a meeting, held alerts are released one at a time, `after_meeting_gap_minutes` apart.
- **Routine** (`info`) alerts are rate-limited (`min_gap_minutes`, `max_per_day`). Limits and important alerts bypass the rate limit but not the snooze or the meeting hold.
- **Presentation** is chosen per level from three styles:
  - `banner`: `osascript display notification`.
  - `dialog`: `osascript display dialog` on a worker thread, which closes itself after 2 minutes.
  - `overlay`: a non-activating `NSPanel` at status-window level that joins every Space and stays until it's closed.
- **The wind-down prompt** needs a text field, so it is always a system dialog. Its result reaches the main thread through `self.pending`, because AppKit and SQLite state are only touched on the main thread.

## Dashboard and API

`/api/stats?from&to&app&domain&scope` returns everything the page needs in one response: totals and the previous period, daily series, timeline (sessions, meetings, personal time, alerts, top apps per hour), breakdowns, heatmap, week pace, notes and settings. The page re-fetches every 30 s and re-renders from scratch.

Renderers carry over only the state that has to survive a re-render:
- timeline zoom;
- table scroll positions;
- a to-do being typed or edited, and which input has focus.

Unsent text is the only thing that overrides server data, so a change made elsewhere, such as from the chat, appears on the next refresh.

**Icons** are produced locally:
- **App icons:** `NSWorkspace.iconForFile` on the app bundle.
- **Website icons:** read from Chromium-family `Favicons` SQLite caches, opened `mode=ro&immutable=1` so the browser's live database is never locked. Each lookup picks the icon used by the most pages on the site, so a page's temporary icon, like GitHub's build-status badge, loses to the site's usual one. Lookups use `page_url` prefix ranges instead of `LIKE`, because the cache can be 100+ MB.

No request leaves the machine.

## Sample data

`DATASETS` maps `real` to the data folder and `mock` to `data-mock/`. `POST /api/dataset` switches which one the dashboard shows, generating the mock data on first use. The selection is a single in-memory value that resets to `real` on restart.

Every dashboard read and write (`/api/stats`, to-dos, close-day, personal time) and the chat's `WORKTRACKER_HOME` follow it. The sampler and the alert engine always use the real data, so activity is never recorded into the sample. The page keeps a separate chat history and Claude session per dataset.

## Chat ("Ask Claude")

`POST /api/chat {message, session_id?}` runs the user's own Claude Code CLI:

```
claude -p --output-format stream-json --verbose --include-partial-messages
       --restricted --tools "Bash,Read,Grep,Glob"
       --allowedTools "Bash(python3 tools/query.py *),Bash(python3 tools/todo.py *),Read,Grep,Glob"
       --permission-mode dontAsk --strict-mcp-config
       --append-system-prompt "<date, logical day, week range, panel constraints> + CLAUDE.md"
       [--resume <session_id>]
```

- **Instructions:** the working directory is the repo. Restricted mode doesn't auto-load `CLAUDE.md`, so its contents (schema, semantics, ready-made queries) are appended to the system prompt on every turn.
- **Message on stdin:** the message is passed on stdin, never as an argument, so it can't be parsed as a flag.
- **Streaming:** stream-json events are reduced to a small NDJSON protocol (`session`, `text`, `block`, `tool`, `done`, `error`) and streamed over the open HTTP response. The browser reads it with `fetch` and a `ReadableStream`.
- **Conversation state** is Claude Code's own session. The page keeps only `session_id` and the rendered messages, in `localStorage`.
- **Permissions:**
  - `--restricted` ignores user, project and local settings files, so permissions or hooks saved for this folder in interactive use don't apply. It also confines Read, Grep and Glob to the repo folder.
  - `--tools` makes only Bash, Read, Grep and Glob available, so Edit, Write and WebFetch don't exist in the session.
  - `dontAsk` denies anything not on the allowlist instead of prompting, since nobody can answer a prompt here.
  - Bash may run only the two helpers.
- **Reads go through `tools/query.py`:** the database is opened read-only, and a SQLite authorizer allows only `SELECT`, reads, functions and a few schema pragmas. It's a Python script, not the `sqlite3` shell, so there are no dot commands like `.shell`.
- **Writes go through the API:** `tools/todo.py` posts to `/api/todo` and `/api/close` rather than touching SQLite, so chat changes get the same validation and logging as dashboard ones. Each to-do is its own row, so concurrent changes to different items can't overwrite each other.
- **Lifecycle:**
  - At most 2 answers run at once.
  - The CLI runs in its own process group, so cancelling kills it and anything it started.
  - Both pipes are read on separate threads, so a chatty stderr can't stall it.
  - While Claude is quiet, the server sends a `ping` event every 5 s. A closed tab or Stop is therefore noticed within seconds rather than at the 300 s timeout, and a timeout or early exit is reported as an error.
- **Network:** this is the only feature that sends data off the machine. Prompts and whatever Claude reads go to Anthropic under the user's Claude account.

## Security model

The server binds to `127.0.0.1` only. The port is bound before startup is announced and fixed for the process's life, and `install.sh` waits until the dashboard answers. Every request must also pass these checks:
- **Host header:** must be `127.0.0.1:<port>` or `localhost:<port>`. This blocks DNS rebinding.
- **POST requests:** must also carry `Content-Type: application/json` and the custom header `X-WT: 1`. Together these force a CORS preflight, and the server never answers one, so no other website open in the browser can post to the local API. This matters most for `/api/chat`, which runs a program.
- **Framing:** every response sends `X-Frame-Options: DENY` and `frame-ancestors 'none'`, so another page can't embed the dashboard to trick clicks.
- **Input limits:** a POST must declare a `Content-Length` of at most 20 KB (otherwise 411 or 413), stalled clients are dropped after 30 s, and bodies must be JSON objects. Icon names are limited to 200 characters, and the icon cache is an LRU of 500 entries. Days must match `YYYY-MM-DD`, chat session ids must be canonical UUIDs, and alert styles must come from the allowed list. Config patches are validated field by field, then written under a lock (temp file, then rename).
- **Trust boundary:** these checks stop websites, not local programs. There's no login, so any process on the Mac, including another user's, can call the API. worktracker assumes a single-user Mac.
- **AppleScript strings** are escaped before being interpolated. Chat output is rendered by a small Markdown converter that escapes all text first and allows only `http(s)` links.

## Storage

| Table | Grain |
|---|---|
| `usage` | (day, hour, app, domain, active, after_hours) → `secs` |
| `sessions` | one row per continuous active stretch |
| `meetings` | one row per Zoom meeting |
| `personal_time` | one row per personal-time block |
| `todos` | one row per to-do: `for_day`, `text`, `done_at`, `done_day` |
| `day_notes` | one row per day: `closed_at` (`next_step` holds the old free-text plans) |
| `alerts` | one row per fired alert, unique per (scope, key) |

Pre-aggregating `usage` per hour keeps the database small (one row per combination per hour, not per sample) while still supporting hourly heatmaps and per-hour timeline details. Second-level precision lives only in `sessions` and `meetings`.

Defaults live in `DEFAULTS` and are merged under `config.json`, so new settings reach existing installs without a migration. On first run the defaults are written to `config.json` as a starting point to edit, so later changes to an existing default don't apply to that install.

The database runs in WAL mode, so recent writes can sit in `tracker.db-wal`. A consistent backup copies `data/` while the agent is stopped.

## Design decisions

- **Local first, single process.** A launchd agent with an embedded HTTP server and SQLite means there's no separate service to manage and nothing to sync, and all state lives in one folder.
- **Active time, not desk time.** Idle trimming, lock detection and personal time all push the totals toward real work, so the goals and limits mean something.
- **Postpone, don't drop.** Alerts held by a meeting or a snooze fire later only if they still apply, so you don't get a burst of stale nudges.
- **Plan as a ritual.** Tomorrow's to-dos are what carries into the next morning, and unfinished items roll over instead of disappearing. Closing the day is optional and only marks when after-hours starts.
- **The chat uses the CLI, not the API.** It reuses the user's existing Claude login and CLAUDE.md, needs no key or SDK, and gets Claude Code's permission system for free.

## Limitations

- macOS only (AppKit, Quartz, AppleScript, launchd).
- Zoom detection depends on the undocumented `CptHost` process. Meet and Teams count as work only while they're in front (`idle_exempt_*`), and alerts aren't held for them.
- Safari and Arc private windows can't be told apart from normal ones, so they are recorded.
- `after_hours` is fixed when a row is written. Changing the work window doesn't relabel history, and the timeline colours past days using the current work window.
- `usage` stores local dates and clock hours without a time zone, while sessions and meetings are timestamps. After changing time zone, older hours and the timeline can disagree.
- Browser URLs are read with a synchronous AppleScript call on the main thread, so a busy browser or an open permission prompt can delay a tick.
- The API has no authentication; it assumes a single-user Mac.
