# worktracker

macOS activity tracker: a launchd agent (`tracker.py`) samples the front app, browser domain and keyboard/mouse idle time every ~10s, stores aggregates in SQLite, serves `dashboard.html` on http://127.0.0.1:8765, and raises work/life-balance alerts.

## Answering questions about the data

The user asks things like "how did this week go", "when do I lose focus", "how much after-hours work". Answer from the database, concisely:

- Lead with the answer in 1–3 sentences, then a small table if it helps. No raw row dumps.
- Use hours and minutes (`6h 40m`), not seconds.
- Compare against the user's goals in `data/config.json` (`goals`, `work_days`, `work_start`/`work_end`) and against the previous comparable period.
- End with at most one concrete, actionable observation. Say when the data is too thin to conclude anything.

Query read-only and never modify `data/`:

```bash
sqlite3 -readonly -header -column data/tracker.db "<query>"
```

### Schema (`data/tracker.db`)

| Table | Grain | Columns |
|---|---|---|
| `usage` | day × hour × app × domain × active × after_hours | `secs` = seconds accumulated |
| `sessions` | one row per continuous active stretch | `day`, `start`, `end` (unix seconds) |
| `day_notes` | one row per day | `closed_at` (unix, NULL if day not closed), `next_step` (tomorrow's first step) |
| `alerts` | one row per alert fired | `day` (or `W<week-start>` for weekly alerts), `key`, `ts`, `message` |

### Semantics that change the answer

- **Work time = `usage` rows with `active = 1`.** `active = 0` is idle time at the computer and is not work.
- **`day` is a logical day** that starts at `day_starts_at_hour` (default 4am): 1am activity belongs to the previous `day`. `hour` is the real clock hour (0–23), so overnight work shows hours 0–3 under the previous day.
- **Week starts on `week_starts_on`** in config (0 = Monday … 6 = Sunday). Weekday numbers in config use that same Monday=0 scheme; SQLite's `strftime('%w')` uses Sunday=0.
- **`after_hours = 1`** means outside `work_start`–`work_end`, on a non-work day, or after the user closed the day. It is fixed at write time; changing the work window later does not re-label old rows.
- **`domain`** is set only when a supported browser was in front; it is `''` otherwise. `www.` is stripped.
- **App names** come from macOS and can contain invisible characters (WhatsApp is stored as `‎WhatsApp`). Match with `LIKE '%WhatsApp'` rather than `=`.
- **Zoom meetings count as active** even with no input. `loginwindow` is the lock screen; exclude it from "apps used".
- Up to 2026-10-24 ~10:00, each break of 5+ min added up to 5 min of phantom work (the idle threshold). After that the tail is trimmed. Flag this when comparing across that date.

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

-- Did the user close the day, and what did they plan next?
SELECT day, time(closed_at,'unixepoch','localtime') closed, next_step FROM day_notes ORDER BY day DESC LIMIT 7;
```

## Working on the code

- `tracker.py` is the whole backend (sampling, alerts, HTTP API); `dashboard.html` is a single-file UI with no external dependencies; `install.sh` creates `.venv`, writes the launchd plist and (re)starts the agent.
- After changing code, run `./install.sh` to restart the agent. Logs: `data/logs/worktracker.log` and `data/logs/launchd.err`.
- `data/` and `.venv/` are git-ignored and must stay that way: they hold the user's personal activity history.
- The HTTP server binds 127.0.0.1 only, checks the `Host` header, and requires `X-WT: 1` + JSON on POST (CSRF guard). Keep those checks.
- Icons: app icons come from the bundle via NSWorkspace; site icons are read locally from Chromium-family `Favicons` databases. Do not add calls to remote favicon services.
