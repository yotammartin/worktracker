# worktracker

macOS activity tracker: a launchd agent (`tracker.py`) samples the front app, browser domain and keyboard/mouse idle time every ~10s, stores aggregates in SQLite, serves `dashboard.html` on http://127.0.0.1:8765, and raises work/life-balance alerts.

## Answering questions about the data

The user asks things like "how did this week go", "when do I lose focus", "how much after-hours work". Answer from the database, concisely:

- Lead with the answer in 1–3 sentences, then a small table if it helps. No raw row dumps.
- Use hours and minutes (`6h 40m`), not seconds.
- Compare against the user's goals (`python3 tools/query.py --settings`: `goals`, `work_days`, `work_start`/`work_end`) and against the previous comparable period.
- End with at most one concrete, actionable observation. Say when the data is too thin to conclude anything.

Query with the read-only helper. It runs one `SELECT` per call and prints a table (max 200 rows):

```bash
python3 tools/query.py "<query>"
python3 tools/query.py --settings     # effective settings: defaults plus the user's overrides
```

### Managing to-dos and closing the day

The only writes allowed. Use the script, never SQL:

```bash
python3 tools/todo.py list                         # today's and upcoming items, with ids
python3 tools/todo.py add "<item>"                 # for tomorrow (the default)
python3 tools/todo.py add today "<item>"           # or tomorrow / YYYY-MM-DD; one item per line
python3 tools/todo.py done <id>                    # also: drop (closed, not done), undo, remove (delete for good)
python3 tools/todo.py edit <id> "<text>"
python3 tools/todo.py move <id> today|tomorrow|YYYY-MM-DD
python3 tools/todo.py close                        # close today; later activity is after hours
python3 tools/todo.py reopen
```

**By default a new item is for the next day.** "add X", "note X" or "remind me X" goes to tomorrow; use `today` or a date only when the user says so. Find ids with `list` before changing an item. Confirm the result in one line.

### Schema (`tracker.db`)

| Table | Grain | Columns |
|---|---|---|
| `usage` | day × hour × app × domain × active × after_hours | `secs` = seconds accumulated |
| `sessions` | one row per continuous active stretch | `day`, `start`, `end` (unix seconds) |
| `todos` | one row per to-do | `for_day` (logical day it's planned for), `text`, `created_at`, `done_at` (unix, NULL while open), `done_day` (logical day it was closed), `dropped` (1 = closed without being done) |
| `day_notes` | one row per day | `closed_at` (unix, NULL if the day wasn't closed); `next_step` is the old free-text plan, kept as history |
| `alerts` | one row per alert fired | `day` (or `W<week-start>` for weekly alerts), `key`, `ts`, `message` |
| `meetings` | one row per Zoom meeting | `start`, `end` (unix, to ~10s), `source` (`zoom` = detected live; any other value was imported) |
| `personal_time` | one row per personal-time block | `start`, `planned_end`, `ended_at` (unix; NULL while running) |

### Semantics that change the answer

- **Work time = `usage` rows with `active = 1`.** `active = 0` is idle time at the computer and is not work.
- **`day` is a logical day** that starts at `day_starts_at_hour` (default 4am): 1am activity belongs to the previous `day`. `hour` is the real clock hour (0–23), so overnight work shows hours 0–3 under the previous day.
- **Week starts on `week_starts_on`** in config (0 = Monday … 6 = Sunday, so `6` means weeks run Sunday–Saturday). Weekday numbers in config use that same Monday=0 scheme; SQLite's `strftime('%w')` uses Sunday=0.
- **Open to-dos carry over:** today's list is every open item with `for_day <= today`, plus such items closed today; a closed item stays in the list of its own `for_day`. An item with an earlier `for_day` is overdue, not lost. Closing the day (`closed_at`) only marks when after-hours started; it doesn't touch to-dos.
- **`after_hours = 1`** means outside `work_start`–`work_end`, on a non-work day, or after the user closed the day. It is fixed at write time; changing the work window later does not re-label old rows.
- **`domain`** is set only when a supported browser was in front; it is `''` otherwise. `www.` is stripped. Chromium incognito windows also store `''`, so a large `''` share under a browser app is private browsing, not a bug. Safari and Arc private windows are recorded normally.
- **Personal time is a gap, not idle:** nothing is written to `usage` or `sessions` while a `personal_time` block runs. Account for it before reading gaps as breaks.
- **App names** come from macOS and can contain invisible characters (WhatsApp is stored as `‎WhatsApp`). Match with `LIKE '%WhatsApp'` rather than `=`.
- **Zoom meetings count as active** even with no input, unless the screen is locked. `usage` shows only which app was in front during a call; use `meetings` for meeting time. If `meetings` has no rows before some date, meeting recording simply wasn't available then. `loginwindow` is the lock screen; exclude it from "apps used".

### Useful queries

```sql
-- Work per day, split in-hours / after-hours (last 14 days)
SELECT day,
       ROUND(SUM(CASE WHEN after_hours=0 THEN secs ELSE 0 END)/3600.0, 2) AS in_h,
       ROUND(SUM(CASE WHEN after_hours=1 THEN secs ELSE 0 END)/3600.0, 2) AS after_h
FROM usage WHERE active=1 AND day >= date('now','-14 days') GROUP BY day ORDER BY day;

-- Top apps and sites for a range
SELECT app, ROUND(SUM(secs)/3600.0, 2) h FROM usage
WHERE active=1 AND day BETWEEN :from AND :to GROUP BY app ORDER BY h DESC LIMIT 10;
SELECT domain, ROUND(SUM(secs)/3600.0, 2) h FROM usage
WHERE active=1 AND domain<>'' AND day BETWEEN :from AND :to GROUP BY domain ORDER BY h DESC LIMIT 10;

-- Focus: session count, longest stretch, first start and last end per day
SELECT day, COUNT(*) sessions, ROUND(MAX(end-start)/60) longest_min,
       time(MIN(start),'unixepoch','localtime') first, time(MAX(end),'unixepoch','localtime') last
FROM sessions GROUP BY day ORDER BY day DESC LIMIT 14;

-- Hour-of-day profile
SELECT hour, ROUND(SUM(secs)/3600.0, 2) h FROM usage WHERE active=1 GROUP BY hour ORDER BY hour;

-- When was each day closed?
SELECT day, time(closed_at,'unixepoch','localtime') closed FROM day_notes WHERE closed_at IS NOT NULL ORDER BY day DESC LIMIT 7;

-- To-dos done per day, and how long items waited
SELECT done_day, COUNT(*) done, ROUND(AVG(julianday(done_day) - julianday(for_day)), 1) avg_days_late
FROM todos WHERE done_at IS NOT NULL AND dropped = 0 GROUP BY done_day ORDER BY done_day DESC LIMIT 14;
```

## Working on the code

- `tracker.py` is the whole backend (sampling, alerts, HTTP API); `dashboard.html` is a single-file UI with no external dependencies; `install.sh` creates `.venv`, writes the launchd plist and (re)starts the agent.
- After changing code, run `./install.sh` to restart the agent. Logs: `data/logs/worktracker.log` and `data/logs/launchd.err`.
- The dashboard chat runs `claude --restricted` here and may only run `tools/query.py` and `tools/todo.py`. Keep data access in those helpers rather than widening the chat's allowed tools.
- `data/` and `.venv/` are git-ignored and must stay that way: they hold the user's personal activity history.
- The HTTP server binds 127.0.0.1 only, checks the `Host` header, and requires `X-WT: 1` + JSON on POST (CSRF guard). Keep those checks.
- Icons: app icons come from the bundle via NSWorkspace; site icons are read locally from Chromium-family `Favicons` databases. Do not add calls to remote favicon services.
