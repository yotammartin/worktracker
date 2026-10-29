#!/usr/bin/env python3
"""Generate a realistic, randomised tracker database for trying out the dashboard.

Usage: .venv/bin/python tools/make_mock_data.py [--out data-mock] [--weeks 3] [--seed N] [--copy-config]

Writes <out>/tracker.db (replacing any existing mock database) with default settings.
--copy-config copies your own config.json instead, so goals and the work window match
yours; that file may hold personal settings, so don't share the folder then.
Refuses to write into the live data folder. Days follow the config: work days get a
full day with meetings, breaks and the odd evening; other days are light or empty.
Today is generated up to the current time.
"""
import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tracker  # noqa: E402  (schema, defaults and day/after-hours rules)

APPS = [("PhpStorm", 30), ("Google Chrome", 30), ("Slack", 18), ("Warp", 8),
        ("WhatsApp", 4), ("Notion", 4), ("Finder", 2), ("Spotify", 1)]
SITES = [("github.com", 25), ("app.clickup.com", 15), ("chatgpt.com", 15),
         ("mail.google.com", 8), ("stackoverflow.com", 6), ("docs.google.com", 6),
         ("google.com", 4), ("x.com", 5), ("youtube.com", 3), ("linkedin.com", 3)]
EVENING_APPS = [("Google Chrome", 50), ("WhatsApp", 20), ("PhpStorm", 15), ("Slack", 15)]
EVENING_SITES = [("youtube.com", 30), ("x.com", 25), ("github.com", 20), ("chatgpt.com", 25)]
MEETING_APPS = [("Zoom", 55), ("Slack", 20), ("Google Chrome", 25)]
TODOS = [
    "Finish the review comments on the ingestion PR", "Write the migration for the new index",
    "Ping the team about the rollout", "Fix the flaky test in CI", "Draft the export design review",
    "Reply to the open threads in #backend", "Pair on the cache bug", "Prepare the sprint demo",
    "Update the on-call runbook", "Book the 1:1 with the new hire",
]


def pick(rng, weighted):
    names, weights = zip(*weighted)
    return rng.choices(names, weights)[0]


def plan_day(rng, d, cfg, is_today_until):
    """Return (minute -> (app, domain, active)) for one calendar day, plus meetings,
    personal-time blocks and the close time."""
    workday = d.weekday() in cfg["work_days"]
    base = datetime(d.year, d.month, d.day)
    at = lambda h, m=0: base + timedelta(hours=h, minutes=m)
    blocks = []                                   # (start, end, mode)
    meetings, personal, closed_at = [], [], None
    if workday:
        start = at(8, 30) + timedelta(minutes=rng.randint(0, 70))
        end = at(17, 30) + timedelta(minutes=rng.randint(0, 110))
        blocks.append((start, end, "work"))
        for _ in range(rng.choice([1, 1, 2, 2, 3])):
            m0 = at(9, 30) + timedelta(minutes=rng.randrange(0, 7 * 60, 5))
            meetings.append((m0, m0 + timedelta(minutes=rng.choice([25, 30, 45, 50, 60]))))
        if rng.random() < 0.25:
            p0 = at(14, 0) + timedelta(minutes=rng.randrange(0, 150, 5))
            personal.append((p0, p0 + timedelta(minutes=rng.choice([30, 60]))))
        if rng.random() < 0.7:
            closed_at = end + timedelta(minutes=rng.randint(0, 5))
        if rng.random() < 0.3:
            e0 = at(20, 30) + timedelta(minutes=rng.randint(0, 90))
            blocks.append((e0, e0 + timedelta(minutes=rng.randint(20, 80)), "evening"))
        if rng.random() < 0.07:
            n0 = at(23, 30) + timedelta(minutes=rng.randint(0, 60))
            blocks.append((n0, n0 + timedelta(minutes=rng.randint(30, 70)), "evening"))
    elif rng.random() < 0.45:
        w0 = at(10) + timedelta(minutes=rng.randint(0, 9 * 60))
        blocks.append((w0, w0 + timedelta(minutes=rng.randint(30, 120)), "evening"))

    minutes = {}
    lunch_at = at(12, 15) + timedelta(minutes=rng.randint(0, 60))
    for start, end, mode in blocks:
        t = start
        app, domain, left = None, "", 0
        stretch_left = rng.randint(25, 100)
        while t < end and t < is_today_until:
            if mode == "work" and t >= lunch_at:
                lunch_at = datetime.max
                lunch = rng.randint(30, 55)
                for i in range(lunch):
                    if t + timedelta(minutes=i) >= is_today_until:
                        break
                    minutes[t + timedelta(minutes=i)] = ("loginwindow", "", False)
                t += timedelta(minutes=lunch)
                stretch_left = rng.randint(25, 100)
                continue
            if stretch_left <= 0:
                brk = rng.randint(6, 25)
                for i in range(brk):
                    if t + timedelta(minutes=i) >= is_today_until:
                        break
                    minutes[t + timedelta(minutes=i)] = ("loginwindow" if brk >= 10 else app or "Finder", "", False)
                t += timedelta(minutes=brk)
                stretch_left = rng.randint(25, 100)
                continue
            if left <= 0:
                app = pick(rng, EVENING_APPS if mode == "evening" else APPS)
                domain = pick(rng, EVENING_SITES if mode == "evening" else SITES) if app == "Google Chrome" else ""
                left = rng.randint(3, 20)
            minutes[t] = (app, domain, True)
            left -= 1
            stretch_left -= 1
            t += timedelta(minutes=1)
    for m0, m1 in meetings:                      # meetings are active whatever is in front
        t = m0
        while t < m1 and t < is_today_until:
            app = pick(rng, MEETING_APPS)
            minutes[t] = (app, rng.choice(["github.com", "app.clickup.com", "docs.google.com"]) if app == "Google Chrome" else "", True)
            t += timedelta(minutes=1)
    for p0, p1 in personal:                      # personal time is not recorded at all
        t = p0
        while t < p1:
            minutes.pop(t, None)
            t += timedelta(minutes=1)
    meetings = [(m0, min(m1, is_today_until)) for m0, m1 in meetings if m0 < is_today_until]
    personal = [p for p in personal if p[0] < is_today_until]
    if closed_at and closed_at > is_today_until:
        closed_at = None
    return minutes, meetings, personal, closed_at


def is_after(cfg, t, closed_at):
    if t.weekday() not in cfg["work_days"]:
        return True
    m = t.hour * 60 + t.minute
    if m < tracker.hm(cfg["work_start"]) or m >= tracker.hm(cfg["work_end"]):
        return True
    return bool(closed_at and t >= closed_at)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data-mock"))
    ap.add_argument("--weeks", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--copy-config", action="store_true", help="use your own config.json")
    args = ap.parse_args()
    if args.weeks < 1:
        ap.error("--weeks must be at least 1")
    rng = random.Random(args.seed)

    out = Path(args.out).resolve()
    live = {(ROOT / "data").resolve()}
    if os.environ.get("WORKTRACKER_HOME"):
        live.add(Path(os.environ["WORKTRACKER_HOME"]).resolve())
    if out in live:
        sys.exit(f"Refusing to write mock data into {out}: that is a live data folder.")
    real_config = Path(os.environ.get("WORKTRACKER_HOME", ROOT / "data")) / "config.json"
    cfg = json.loads(json.dumps(tracker.DEFAULTS))
    if args.copy_config and real_config.exists():
        for k, v in json.loads(real_config.read_text()).items():
            cfg[k] = {**cfg[k], **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    tracker.cfg = cfg

    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("tracker.db*"):
        f.unlink()
    if args.copy_config and real_config.exists():
        shutil.copy(real_config, out / "config.json")
    else:
        (out / "config.json").write_text(json.dumps(cfg, indent=2))

    conn = sqlite3.connect(out / "tracker.db")
    tracker.init_db(conn)
    now = datetime.now().replace(second=0, microsecond=0)
    today = now.date()
    first = tracker.week_start_of(today) - timedelta(weeks=args.weeks - 1)
    usage, closed_by_day = {}, {}
    totals = {}
    for i in range((today - first).days + 1):
        d = first + timedelta(days=i)
        minutes, meetings, personal, closed_at = plan_day(rng, d, cfg, now)
        logical = d.isoformat()
        for t in sorted(minutes):
            app, domain, active = minutes[t]
            day = tracker.day_key(t.timestamp())
            key = (day, t.hour, app, domain, int(active), int(is_after(cfg, t, closed_at)))
            usage[key] = usage.get(key, 0) + 60
            if active:
                totals.setdefault(day, []).append(t)
        run_start = prev = None
        for t in sorted(t for t, v in minutes.items() if v[2]) + [None]:
            if run_start and (t is None or t - prev > timedelta(minutes=1)):
                conn.execute("INSERT INTO sessions(day, start, end) VALUES(?,?,?)",
                             (tracker.day_key(run_start.timestamp()), run_start.timestamp(),
                              (prev + timedelta(minutes=1)).timestamp()))
                run_start = None
            if t and run_start is None:
                run_start = t
            prev = t
        for m0, m1 in meetings:
            conn.execute("INSERT INTO meetings(start, end, source) VALUES(?,?,'mock')",
                         (m0.timestamp(), m1.timestamp()))
        for p0, p1 in personal:
            conn.execute("INSERT INTO personal_time(start, planned_end, ended_at) VALUES(?,?,?)",
                         (p0.timestamp(), p1.timestamp(), p1.timestamp() if p1 <= now else None))
        if closed_at:
            closed_by_day[logical] = closed_at.timestamp()
        if d.weekday() in cfg["work_days"]:
            # A few to-dos planned for each work day; most get done that day.
            for text in rng.sample(TODOS, rng.randint(2, 4)):
                done = d < today - timedelta(days=1) or rng.random() < 0.5
                done_at = (datetime.combine(d, datetime.min.time()) + timedelta(hours=rng.uniform(10, 17))).timestamp()
                conn.execute("INSERT INTO todos(for_day, text, created_at, done_at, done_day) VALUES(?,?,?,?,?)",
                             (logical, text, done_at - 86400, done_at if done and done_at <= now.timestamp() else None,
                              logical if done and done_at <= now.timestamp() else None))
    conn.executemany("INSERT INTO usage VALUES(?,?,?,?,?,?,?)",
                     [(*k, v) for k, v in usage.items()])
    for day, closed in closed_by_day.items():
        conn.execute("INSERT INTO day_notes(day, closed_at) VALUES(?,?)", (day, closed))
    tomorrow = (today + timedelta(days=1)).isoformat()
    conn.executemany("INSERT INTO todos(for_day, text, created_at) VALUES(?,?,?)",
                     [(tomorrow, text, now.timestamp()) for text in rng.sample(TODOS, 2)])
    target = cfg["goals"]["daily_target_hours"] * 60
    for day, active_minutes in totals.items():
        if target > 0 and len(active_minutes) >= target:
            hit = active_minutes[int(target) - 1]
            conn.execute("INSERT INTO alerts VALUES(?,?,?,?)", (
                day, "target", hit.timestamp(),
                f"Target reached at {hit:%H:%M} ({len(active_minutes) // 60}h active). A good place to stop."))
        evening = [t for t in active_minutes if t.hour >= 20 or t.hour < 4]
        if len(evening) >= 15:
            t = evening[14]
            conn.execute("INSERT INTO alerts VALUES(?,?,?,?)", (
                day, f"after1:{int(t.timestamp())}", t.timestamp(),
                f"It's {t:%H:%M}, outside your workday. 15 min active so far. Urgent, or can it wait?"))
    conn.commit()
    days = len(totals)
    hours = sum(len(v) for v in totals.values()) / 60
    print(f"Wrote {out / 'tracker.db'}: {days} active days, {hours:.0f}h of work, "
          f"{first} to {today}")


if __name__ == "__main__":
    main()
